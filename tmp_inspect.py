import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import pandas as pd

df = pd.read_excel(r'C:\Users\Salo\Downloads\contactos_limpios.xlsx')

def classify_lead(lead):
    if pd.isna(lead):
        return 'null'
    s = str(lead).strip()
    if '@' in s:
        return 'facebook_lid'
    try:
        n = int(float(s))
        ns = str(n)
        if len(ns) > 15:
            return 'messenger_id'
        if ns.startswith('57') and len(ns) >= 12:
            return 'co_phone'
        if len(ns) >= 10:
            return 'other_phone'
    except Exception:
        pass
    return 'unknown'

df['lead_type'] = df['Lead'].apply(classify_lead)

print('=== CLASIFICACION DE LEADS ===')
print(df['lead_type'].value_counts().to_string())
print()
print('=== CANAL ===')
print(df['Channel'].value_counts().to_string())
print()

non_zero = df[df['Value'].notna()]
non_zero = non_zero[non_zero['Value'] != 0]
print(f'Contactos con Value distinto de 0: {len(non_zero)}')
if len(non_zero) > 0:
    print(non_zero[['Name', 'Value', 'Funnel Name']].head(10).to_string())
print()

migrables = df[df['lead_type'].isin(['co_phone', 'other_phone'])]
print(f'=== MIGRABLES (telefonos reales): {len(migrables)} ===')
print(migrables['Funnel Name'].value_counts().to_string())
print()

# Muestra de telefonos migrables
print('=== MUESTRA TELEFONOS MIGRABLES (primeros 8) ===')
print(migrables[['Lead', 'Name', 'Funnel Name', 'Stage Name', 'Channel']].head(8).to_string())
