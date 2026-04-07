import json
import csv
import os
import re
from netmiko import ConnectHandler

# --- CONFIGURATIE ---
SERIAL_PORT = '/dev/ttyUSB0'

OUTPUT_MAP = '/home/devasc/inventory'
CSV_FILENAME = os.path.join(OUTPUT_MAP, 'inventory_overzicht.csv')

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

    # --- FIX 1: Longer timeouts for routers (they boot & respond slower) ---
    'auth_timeout': 30,            # time allowed for enable/auth

    # --- FIX 2: Global delay factor slows down ALL interactions ---
    # Increase this if you still get timeouts (try 2 or 3)
    'global_delay_factor': 2,
}

SESSION_LOG = os.path.join(OUTPUT_MAP, 'netmiko_session.log')


def handle_initial_dialog(connection):
    """
    Handle the IOS setup wizard that appears on unconfigured routers.
    'Would you like to enter the initial configuration dialog? [yes/no]:'
    Also handles rommon / boot prompts.
    """
    output = connection.read_channel()
    if 'initial configuration dialog' in output.lower():
        print("  [INFO] Setup wizard detected, answering 'no'...")
        connection.write_channel('no\n')
        import time; time.sleep(3)
        output = connection.read_channel()
    if 'press return to get started' in output.lower():
        connection.write_channel('\n')
        import time; time.sleep(2)


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

    clei = re.search(r'CLEI:\s*(\S+)', inventory_raw)
    clei = clei.group(1) if clei else None

    return hostname, os_versie, sn, pid, clei


def main():
    os.makedirs(OUTPUT_MAP, exist_ok=True)

    tag_id = input("Voer het fysieke label/tag in voor dit device (bijv. INV-001): ")
    temp_filepath = os.path.join(OUTPUT_MAP, f"{tag_id}_raw.json")

    print(f"[{tag_id}] Verbinden via console op {SERIAL_PORT}...")
    print(f"         (sessie log wordt opgeslagen in {SESSION_LOG})")

    try:
        # FIX 3: Write a session log so you can see exactly what the router is sending
        connection = ConnectHandler(
            **cisco_device,
            session_log=SESSION_LOG,
        )

        # FIX 4: Handle routers that are stuck in setup wizard or need Enter pressed
        handle_initial_dialog(connection)

        connection.enable()

        print(f"[{tag_id}] Verbonden! Gegevens ophalen...")

        # FIX 5: Removed duplicate connection.enable() call from original script
        # FIX 6: Fixed typo use8_genie -> use_genie
        versie_data    = connection.send_command('show version',   use_genie=True)
        inventory_data = connection.send_command('show inventory', use_genie=True)
        versie_raw     = connection.send_command('show version')
        inventory_raw  = connection.send_command('show inventory')

        connection.disconnect()
        print(f"[{tag_id}] Verbinding gesloten. Gegevens verwerken...")

        # --- GENIE PARSE ATTEMPT ---
        hostname  = versie_data.get('version', {}).get('hostname', '')  if isinstance(versie_data,    dict) else ''
        os_versie = versie_data.get('version', {}).get('version',  '')  if isinstance(versie_data,    dict) else ''

        pid  = ''
        sn   = ''
        clei = None

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
                    pid  = details.get('pid', '')
                    sn   = details.get('sn', '')
                    clei = details.get('clei_code_num') or details.get('clei')
                    break

        # --- REGEX FALLBACK if Genie returned incomplete data ---
        if not hostname or not sn or not os_versie:
            print(f"[{tag_id}] Genie parser onvolledig, regex fallback gebruiken...")
            fb_hostname, fb_os, fb_sn, fb_pid, fb_clei = extract_with_regex(versie_raw, inventory_raw)
            hostname  = hostname  or fb_hostname
            os_versie = os_versie or fb_os
            sn        = sn        or fb_sn
            pid       = pid       or fb_pid
            clei      = clei      or fb_clei

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

        # --- APPEND TO CSV ---
        file_exists = os.path.isfile(CSV_FILENAME)

        with open(CSV_FILENAME, 'a', newline='') as csv_file:
            writer = csv.writer(csv_file, quoting=csv.QUOTE_ALL)
            if not file_exists:
                writer.writerow(['Tag ID', 'Hostname', 'Model (PID)', 'Serienummer (SN)', 'CLEI', 'OS Versie'])
            writer.writerow([tag_id, hostname, pid, sn, clei or 'N/A', os_versie])

        print(f"[{tag_id}] Succesvol toegevoegd aan {CSV_FILENAME}!")
        print(f"       Hostname: {hostname} | PID: {pid} | SN: {sn} | OS: {os_versie}\n")
        print("-" * 50)

    except Exception as e:
        print(f"\n[FOUT] Er ging iets mis met device {tag_id}:")
        print(e)
        print(f"\nCheck het sessie log voor details: {SESSION_LOG}")
        print("Veelvoorkomende oorzaken bij routers:")
        print("  1. Router zit in setup wizard -> druk eerst handmatig Enter/Ctrl+C via minicom")
        print("  2. Enable password vereist -> voeg 'secret' toe aan cisco_device dict")
        print("  3. Router reageert traag -> verhoog global_delay_factor naar 3 of 4")
        print("  4. Verkeerde baudrate -> probeer 115200 als router dat gebruikt\n")


if __name__ == '__main__':
    main()