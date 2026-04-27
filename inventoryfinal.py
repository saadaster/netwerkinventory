import json
import csv
import os
import re
import time
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

# --- CONFIGURATIE ---
SERIAL_PORT = '/dev/ttyUSB0'

OUTPUT_MAP = '/home/devasc/inventory'
CSV_FILENAME = os.path.join(OUTPUT_MAP, 'inventory_overzicht.csv')
SESSION_LOG = os.path.join(OUTPUT_MAP, 'netmiko_session.log')

cisco_device = {
    'device_type': 'cisco_ios_serial',
    'serial_settings': {
        'port': SERIAL_PORT,
        'baudrate': 9600,
        'bytesize': 8,
        'parity': 'N',
        'stopbits': 1
    },
    'fast_cli': False,
    'timeout': 30,
    'auth_timeout': 30,
    'global_delay_factor': 2,
}


def strip_syslog(output):
    """
    Remove IOS syslog lines from command output.
    Lines starting with '*' (timestamped) or '%' (error/info codes)
    can corrupt Genie parsing or confuse prompt detection.
    """
    clean = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith('*') or stripped.startswith('%'):
            continue
        clean.append(line)
    return '\n'.join(clean)


def handle_initial_dialog(connection):
    """
    Handle the IOS setup wizard that appears on unconfigured routers.
    'Would you like to enter the initial configuration dialog? [yes/no]:'
    Also handles 'Press RETURN to get started' prompts.
    """
    output = connection.read_channel()
    if 'initial configuration dialog' in output.lower():
        print("  [INFO] Setup wizard detected, answering 'no'...")
        connection.write_channel('no\n')
        time.sleep(3)
        output = connection.read_channel()
    if 'press return to get started' in output.lower():
        connection.write_channel('\n')
        time.sleep(2)


def extract_with_regex(versie_raw, inventory_raw):
    """Fallback: parse plain text output with regex when Genie fails."""
    hostname = re.search(r'^(\S+)\s+uptime', versie_raw, re.MULTILINE)
    hostname = hostname.group(1) if hostname else 'Onbekend'

    os_versie = re.search(r'Cisco IOS.*Version\s+(\S+)', versie_raw)
    os_versie = os_versie.group(1) if os_versie else 'Onbekend'

    # Try 'show inventory' first for SN
    sn = re.search(r'SN:\s*(\S+)', inventory_raw)
    sn = sn.group(1) if sn else None

    # Fallback: grab SN from 'show version'
    if not sn:
        sn = re.search(r'[Ss]ystem [Ss]erial [Nn]umber\s*:\s*(\S+)', versie_raw)
        sn = sn.group(1) if sn else 'Onbekend'

    pid = re.search(r'PID:\s*(\S+)', inventory_raw)
    pid = pid.group(1) if pid else 'Onbekend'

    return hostname, os_versie, sn, pid


def get_existing_sns():
    """Read all serial numbers already in the CSV to detect duplicates."""
    existing = set()
    if os.path.isfile(CSV_FILENAME):
        with open(CSV_FILENAME, 'r', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                sn = row.get('Serienummer (SN)', '').strip()
                if sn and sn != 'Onbekend':
                    existing.add(sn)
    return existing


def main():
    os.makedirs(OUTPUT_MAP, exist_ok=True)

    tag_id = input("Voer het fysieke label/tag in voor dit device (bijv. INV-001): ")
    temp_filepath = os.path.join(OUTPUT_MAP, f"{tag_id}_raw.json")

    print(f"[{tag_id}] Verbinden via console op {SERIAL_PORT}...")
    print(f"         (sessie log wordt opgeslagen in {SESSION_LOG})")

    try:
        connection = ConnectHandler(
            **cisco_device,
            session_log=SESSION_LOG,
        )

        handle_initial_dialog(connection)
        connection.enable()

        # Disable console syslog flooding for this session
        connection.send_command('terminal no monitor')

        print(f"[{tag_id}] Verbonden! Gegevens ophalen...")

        # FIX 3: Get raw output once, pass to both Genie and regex fallback
        versie_raw    = connection.send_command('show version')
        inventory_raw = connection.send_command('show inventory')

        # Strip any syslog lines that slipped through
        versie_raw    = strip_syslog(versie_raw)
        inventory_raw = strip_syslog(inventory_raw)

        # Parse with Genie using the raw text we already have
        versie_data    = connection.send_command('show version',   use_genie=True)
        inventory_data = connection.send_command('show inventory', use_genie=True)

        connection.disconnect()
        print(f"[{tag_id}] Verbinding gesloten. Gegevens verwerken...")

        # --- GENIE PARSE ATTEMPT ---
        hostname  = versie_data.get('version', {}).get('hostname', '') if isinstance(versie_data, dict) else ''
        os_versie = versie_data.get('version', {}).get('version',  '') if isinstance(versie_data, dict) else ''

        pid  = ''
        sn   = ''

        if isinstance(inventory_data, dict) and inventory_data:
            chassis_info = {}
            if 'main' in inventory_data and 'chassis' in inventory_data['main']:
                chassis_info = inventory_data['main']['chassis']
            elif 'slot' in inventory_data:
                chassis_info = inventory_data['slot']
            elif 'name' in inventory_data:
                chassis_info = inventory_data['name']
            else:
                chassis_info = inventory_data

            for key, details in chassis_info.items():
                if isinstance(details, dict) and details.get('sn'):
                    pid = details.get('pid', '')
                    sn  = details.get('sn', '')
                    break

        # --- REGEX FALLBACK if Genie returned incomplete data ---
        if not hostname or not sn or not os_versie:
            print(f"[{tag_id}] Genie parser onvolledig, regex fallback gebruiken...")
            fb_hostname, fb_os, fb_sn, fb_pid = extract_with_regex(versie_raw, inventory_raw)
            hostname  = hostname  or fb_hostname
            os_versie = os_versie or fb_os
            sn        = sn        or fb_sn
            pid       = pid       or fb_pid

        # FIX 4: Check for duplicate SN before writing
        existing_sns = get_existing_sns()
        if sn and sn != 'Onbekend' and sn in existing_sns:
            print(f"[{tag_id}] WAARSCHUWING: Serienummer '{sn}' staat al in de CSV!")
            print(f"           Dit device is mogelijk al eerder gescand. Rij NIET toegevoegd.")
            return

        # --- SAVE RAW JSON ---
        raw_data = {
            'show_version':       versie_data,
            'show_inventory':     inventory_data,
            'show_version_raw':   versie_raw,
            'show_inventory_raw': inventory_raw,
        }

        with open(temp_filepath, 'w') as f:
            json.dump(raw_data, f, indent=4)

        # --- RENAME TO SN IF FOUND ---
        if sn and sn != 'Onbekend':
            final_filepath = os.path.join(OUTPUT_MAP, f"{sn}_raw.json")
            os.rename(temp_filepath, final_filepath)
            print(f"[{tag_id}] Bestand hernoemd naar '{os.path.basename(final_filepath)}'")
        else:
            final_filepath = temp_filepath
            print(f"[{tag_id}] Geen SN gevonden, bestand bewaard als '{os.path.basename(final_filepath)}'")

        # --- APPEND TO CSV (FIX 6: CLEI column removed) ---
        file_exists = os.path.isfile(CSV_FILENAME)

        with open(CSV_FILENAME, 'a', newline='') as csv_file:
            writer = csv.writer(csv_file, quoting=csv.QUOTE_ALL)
            if not file_exists:
                writer.writerow(['Tag ID', 'Hostname', 'Model (PID)', 'Serienummer (SN)', 'OS Versie'])
            writer.writerow([tag_id, hostname, pid, sn, os_versie])

        print(f"[{tag_id}] Succesvol toegevoegd aan {CSV_FILENAME}!")
        print(f"       Hostname: {hostname} | PID: {pid} | SN: {sn} | OS: {os_versie}\n")
        print("-" * 50)

    # FIX 5: Specific exception handling instead of bare except
    except NetmikoTimeoutException:
        print(f"\n[FOUT] Timeout bij verbinding met device {tag_id}.")
        print(f"Check het sessie log voor details: {SESSION_LOG}")
        print("Mogelijke oorzaken:")
        print("  1. Router reageert traag -> verhoog global_delay_factor naar 3 of 4")
        print("  2. Verkeerde baudrate -> probeer 115200")
        print("  3. Console kabel niet goed aangesloten\n")

    except NetmikoAuthenticationException:
        print(f"\n[FOUT] Authenticatie mislukt bij device {tag_id}.")
        print("Mogelijke oorzaken:")
        print("  1. Enable password vereist -> voeg 'secret' toe aan cisco_device dict")
        print("  2. Router zit in setup wizard -> druk eerst handmatig Enter/Ctrl+C via minicom\n")

    except Exception as e:
        print(f"\n[FOUT] Onverwachte fout bij device {tag_id}: {type(e).__name__}")
        print(f"Details: {e}")
        print(f"\nCheck het sessie log voor details: {SESSION_LOG}\n")


if __name__ == '__main__':
    main()