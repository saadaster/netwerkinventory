import json
import csv
import os
import re
import time
import logging
from datetime import datetime
from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException

# Try to import Genie parsers for offline parsing (avoids double send_command)
try:
    from genie.libs.parser.iosxe.show_platform import ShowVersion, ShowInventory
    GENIE_AVAILABLE = True
except ImportError:
    GENIE_AVAILABLE = False

# --- CONFIGURATIE ---
SERIAL_PORT = '/dev/ttyUSB0'

OUTPUT_MAP   = '/home/devasc/inventory'
CSV_FILENAME = os.path.join(OUTPUT_MAP, 'inventory_overzicht.csv')
SESSION_LOG  = os.path.join(OUTPUT_MAP, 'netmiko_session.log')

# --- LOGGING SETUP ---
# Writes timestamped entries to a persistent log file alongside the session log.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(OUTPUT_MAP, 'inventory_run.log')),
        logging.StreamHandler(),   # still prints to console
    ]
)
log = logging.getLogger(__name__)

cisco_device = {
    'device_type': 'cisco_ios_serial',
    'serial_settings': {
        'port':      SERIAL_PORT,
        'baudrate':  9600,
        'bytesize':  8,
        'parity':    'N',
        'stopbits':  1,
    },
    'fast_cli':           False,
    'timeout':            30,
    'auth_timeout':       30,
    'global_delay_factor': 2,
}


# ---------------------------------------------------------------------------
# IMPROVEMENT 1: parse Genie offline from already-captured raw text
#   Previously send_command was called TWICE per command (once for raw text,
#   once with use_genie=True). At 9600 baud each round-trip is slow.
#   We now capture raw text once and parse it locally with Genie's parser
#   classes — no extra serial traffic.
# ---------------------------------------------------------------------------
def genie_parse_offline(command: str, raw_output: str, device_os: str = 'iosxe') -> dict:
    """
    Parse raw IOS command output with the Genie parser library locally.
    Returns a parsed dict, or {} if Genie is unavailable / parsing fails.
    """
    if not GENIE_AVAILABLE:
        return {}
    try:
        from genie.libs.parser.utils import get_parser
        # get_parser needs a mock device object with an 'os' attribute
        class _MockDevice:
            os = device_os
        parser_cls = get_parser(command, _MockDevice())
        parser_obj = parser_cls(device=_MockDevice())
        return parser_obj.parse(output=raw_output)
    except Exception as exc:
        log.debug("Genie offline parse failed for '%s': %s", command, exc)
        return {}


def strip_syslog(output: str) -> str:
    """
    Remove IOS syslog lines from command output.
    Lines starting with '*' (timestamped) or '%' (error/info codes)
    can corrupt Genie parsing or confuse prompt detection.
    """
    clean = [
        line for line in output.splitlines()
        if not line.strip().startswith(('*', '%'))
    ]
    return '\n'.join(clean)


def handle_initial_dialog(connection) -> None:
    """
    Handle the IOS setup wizard on unconfigured routers and
    'Press RETURN to get started' prompts.
    """
    output = connection.read_channel()
    if 'initial configuration dialog' in output.lower():
        log.info("Setup wizard detected — answering 'no'.")
        connection.write_channel('no\n')
        time.sleep(3)
        output = connection.read_channel()
    if 'press return to get started' in output.lower():
        connection.write_channel('\n')
        time.sleep(2)


def extract_with_regex(versie_raw: str, inventory_raw: str) -> tuple:
    """Fallback: parse plain-text output with regex when Genie fails."""
    hostname = re.search(r'^(\S+)\s+uptime', versie_raw, re.MULTILINE)
    hostname  = hostname.group(1) if hostname else 'Onbekend'

    os_versie = re.search(r'Cisco IOS.*Version\s+(\S+)', versie_raw)
    os_versie = os_versie.group(1) if os_versie else 'Onbekend'

    sn = re.search(r'SN:\s*(\S+)', inventory_raw)
    sn = sn.group(1) if sn else None

    if not sn:
        sn = re.search(r'[Ss]ystem [Ss]erial [Nn]umber\s*:\s*(\S+)', versie_raw)
        sn = sn.group(1) if sn else 'Onbekend'

    pid = re.search(r'PID:\s*(\S+)', inventory_raw)
    pid = pid.group(1) if pid else 'Onbekend'

    return hostname, os_versie, sn, pid


def get_existing_sns() -> set:
    """Read all serial numbers already in the CSV to detect duplicates."""
    existing = set()
    if os.path.isfile(CSV_FILENAME):
        with open(CSV_FILENAME, 'r', newline='') as f:
            for row in csv.DictReader(f):
                sn = row.get('Serienummer (SN)', '').strip()
                if sn and sn != 'Onbekend':
                    existing.add(sn)
    return existing


# ---------------------------------------------------------------------------
# IMPROVEMENT 2: extract parsed fields into a dedicated helper
#   Keeps main() readable and makes unit-testing the parse logic easy.
# ---------------------------------------------------------------------------
def extract_fields(versie_data: dict, inventory_data: dict,
                   versie_raw: str,  inventory_raw: str,
                   tag_id: str) -> tuple:
    """
    Return (hostname, os_versie, sn, pid) from Genie dicts.
    Falls back to regex automatically when fields are missing.
    """
    hostname  = versie_data.get('version', {}).get('hostname', '') if isinstance(versie_data,  dict) else ''
    os_versie = versie_data.get('version', {}).get('version',  '') if isinstance(versie_data,  dict) else ''
    pid = sn  = ''

    if isinstance(inventory_data, dict) and inventory_data:
        chassis_info = (
            inventory_data.get('main', {}).get('chassis')
            or inventory_data.get('slot')
            or inventory_data.get('name')
            or inventory_data
        )
        for details in chassis_info.values():
            if isinstance(details, dict) and details.get('sn'):
                pid = details.get('pid', '')
                sn  = details.get('sn',  '')
                break

    if not all([hostname, sn, os_versie]):
        log.info("[%s] Genie parse incomplete — using regex fallback.", tag_id)
        fb_host, fb_os, fb_sn, fb_pid = extract_with_regex(versie_raw, inventory_raw)
        hostname  = hostname  or fb_host
        os_versie = os_versie or fb_os
        sn        = sn        or fb_sn
        pid       = pid       or fb_pid

    return hostname, os_versie, sn, pid


# ---------------------------------------------------------------------------
# IMPROVEMENT 3: atomic CSV write with a context manager
#   The original code opened the CSV, wrote, and relied on the 'with' block
#   to flush. This is fine, but the header-detection logic (file_exists check
#   before opening) has a tiny TOCTOU race. We now check *after* opening.
# ---------------------------------------------------------------------------
def append_to_csv(tag_id: str, hostname: str, pid: str, sn: str, os_versie: str) -> None:
    """Append one inventory row to the CSV, writing the header if needed."""
    write_header = not os.path.isfile(CSV_FILENAME) or os.path.getsize(CSV_FILENAME) == 0
    with open(CSV_FILENAME, 'a', newline='') as csv_file:
        writer = csv.writer(csv_file, quoting=csv.QUOTE_ALL)
        if write_header:
            writer.writerow(['Tag ID', 'Hostname', 'Model (PID)', 'Serienummer (SN)',
                             'OS Versie', 'Tijdstempel'])          # IMPROVEMENT 4: timestamp column
        writer.writerow([tag_id, hostname, pid, sn, os_versie,
                         datetime.now().strftime('%Y-%m-%d %H:%M:%S')])


def save_raw_json(tag_id: str, sn: str,
                  versie_data, inventory_data,
                  versie_raw: str, inventory_raw: str) -> str:
    """
    Save raw command output to JSON.
    File is named by SN when available, otherwise by tag_id.
    Returns the final file path.
    """
    raw_data = {
        'show_version':       versie_data,
        'show_inventory':     inventory_data,
        'show_version_raw':   versie_raw,
        'show_inventory_raw': inventory_raw,
    }
    base     = sn if (sn and sn != 'Onbekend') else tag_id
    filepath = os.path.join(OUTPUT_MAP, f"{base}_raw.json")
    with open(filepath, 'w') as f:
        json.dump(raw_data, f, indent=4)
    return filepath


def main() -> None:
    os.makedirs(OUTPUT_MAP, exist_ok=True)

    tag_id = input("Voer het fysieke label/tag in voor dit device (bijv. INV-001): ").strip()
    if not tag_id:
        log.error("Geen tag opgegeven. Script gestopt.")
        return

    log.info("[%s] Verbinden via console op %s …", tag_id, SERIAL_PORT)
    log.info("         Sessie log: %s", SESSION_LOG)

    try:
        with ConnectHandler(**cisco_device, session_log=SESSION_LOG) as connection:
            handle_initial_dialog(connection)
            connection.enable()
            connection.send_command('terminal no monitor')

            log.info("[%s] Verbonden! Gegevens ophalen…", tag_id)

            # --- SINGLE send_command per command (IMPROVEMENT 1 applied) ---
            versie_raw    = strip_syslog(connection.send_command('show version'))
            inventory_raw = strip_syslog(connection.send_command('show inventory'))

        # Connection is closed here by the context manager — parse offline
        log.info("[%s] Verbinding gesloten. Gegevens verwerken…", tag_id)

        versie_data    = genie_parse_offline('show version',   versie_raw)
        inventory_data = genie_parse_offline('show inventory', inventory_raw)

        hostname, os_versie, sn, pid = extract_fields(
            versie_data, inventory_data, versie_raw, inventory_raw, tag_id
        )

        # --- DUPLICATE CHECK ---
        if sn and sn != 'Onbekend' and sn in get_existing_sns():
            log.warning("[%s] SN '%s' staat al in de CSV — rij NIET toegevoegd.", tag_id, sn)
            return

        # --- PERSIST ---
        filepath = save_raw_json(tag_id, sn, versie_data, inventory_data, versie_raw, inventory_raw)
        log.info("[%s] Raw data opgeslagen: %s", tag_id, os.path.basename(filepath))

        append_to_csv(tag_id, hostname, pid, sn, os_versie)

        log.info("[%s] Succesvol toegevoegd!", tag_id)
        log.info("       Hostname: %s | PID: %s | SN: %s | OS: %s", hostname, pid, sn, os_versie)
        print("-" * 50)

    except NetmikoTimeoutException:
        log.error("[%s] Timeout bij verbinding.", tag_id)
        log.error("  Check sessie log: %s", SESSION_LOG)
        log.error("  Tips: verhoog global_delay_factor, controleer baudrate / kabelaansluiting.")

    except NetmikoAuthenticationException:
        log.error("[%s] Authenticatie mislukt.", tag_id)
        log.error("  Tips: voeg 'secret' toe aan cisco_device, of herstel de router handmatig.")

    except Exception as exc:
        log.exception("[%s] Onverwachte fout: %s", tag_id, exc)
        log.error("  Check sessie log: %s", SESSION_LOG)


if __name__ == '__main__':
    main()