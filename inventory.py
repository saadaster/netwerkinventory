import json
import csv
import os
from netmiko import ConnectHandler

# --- CONFIGURATIE ---
SERIAL_PORT = 'COM4'  # Verander dit naar jouw poort (bijv. '/dev/ttyUSB0' op Mac)
CSV_FILENAME = 'inventory_overzicht.csv'

# Gegevens voor Netmiko om de seriële verbinding op te zetten
cisco_device = {
    'device_type': 'cisco_ios_serial',
    'serial_settings': {
        'port': SERIAL_PORT,
        'baudrate': 9600,
        'bytesize': 8,
        'parity': 'N',
        'stopbits': 1
    },
    # Als de devices compleet gewist zijn, is er geen wachtwoord nodig.
    # Staan er nog wachtwoorden op? Haal de comments hieronder dan weg:
    # 'username': 'jouw_gebruikersnaam',
    # 'password': 'jouw_wachtwoord',
    # 'secret': 'enable_wachtwoord',
}

def main():
    tag_id = input("Voer het fysieke label/tag in voor dit device (bijv. INV-001): ")
    
    print(f"[{tag_id}] Verbinden via console op {SERIAL_PORT}...")
    
    try:
        # Zet de verbinding op
        connection = ConnectHandler(**cisco_device)
        connection.enable() # Zorgt dat je in de 'enable' mode (privilege exec) komt
        
        print(f"[{tag_id}] Verbonden! Gegevens ophalen met Genie parser...")
        
        # Voer de commando's uit en laat Genie de magie doen
        versie_data = connection.send_command('show version', use_genie=True)
        inventory_data = connection.send_command('show inventory', use_genie=True)
        
        connection.disconnect()
        print(f"[{tag_id}] Verbinding gesloten. Gegevens verwerken...")

        # --- DATA OPSLAAN ALS JSON (Ruwe Backup) ---
        raw_data = {
            'show_version': versie_data,
            'show_inventory': inventory_data
        }
        json_filename = f"{tag_id}_raw.json"
        with open(json_filename, 'w') as json_file:
            json.dump(raw_data, json_file, indent=4)
        print(f"[{tag_id}] Ruwe data opgeslagen in {json_filename}")

        # --- DATA UITLEZEN VOOR CSV ---
        # Let op: de structuur van de dictionary hangt af van het exacte model.
        # We gebruiken .get() zodat het script niet crasht als een veld ontbreekt.
        
        hostname = versie_data.get('version', {}).get('hostname', 'Onbekend')
        os_versie = versie_data.get('version', {}).get('version', 'Onbekend')
        
        # Vaak staat het hoofdchassis onder 'Chassis' of de hoofdsleutel in inventory
        pid = "Onbekend"
        sn = "Onbekend"
        
        if inventory_data and 'main' in inventory_data:
            chassis_info = inventory_data['main']['chassis']
            for name, details in chassis_info.items():
                # We pakken het eerste chassis item dat we tegenkomen
                pid = details.get('pid', 'Onbekend')
                sn = details.get('sn', 'Onbekend')
                break
        
        # --- DATA TOEVOEGEN AAN CSV ---
        file_exists = os.path.isfile(CSV_FILENAME)
        
        with open(CSV_FILENAME, 'a', newline='') as csv_file:
            writer = csv.writer(csv_file)
            # Schrijf de header als het bestand net nieuw is
            if not file_exists:
                writer.writerow(['Tag ID', 'Hostname', 'Model (PID)', 'Serienummer (SN)', 'OS Versie'])
            
            writer.writerow([tag_id, hostname, pid, sn, os_versie])
            
        print(f"[{tag_id}] Succesvol toegevoegd aan {CSV_FILENAME}!\n")
        print("-" * 50)

    except Exception as e:
        print(f"\n[FOUT] Er ging iets mis met device {tag_id}:")
        print(e)
        print("Check of de console kabel goed vastzit en de switch volledig is opgestart.\n")

if __name__ == '__main__':
    main()