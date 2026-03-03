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

# Filtrar los NO migrables (excluir nulos porque no tienen datos útiles)
no_phone = df[df['lead_type'].isin(['facebook_lid', 'messenger_id', 'unknown'])]

# También incluir los de canal INSTAGRAM o MESSENGER aunque tengan telefono
instagram_messenger = df[
    df['Channel'].isin(['INSTAGRAM', 'MESSENGER']) &
    ~df['lead_type'].isin(['facebook_lid', 'messenger_id', 'unknown'])
]

# Unir ambos sets (sin duplicados)
no_phone_full = pd.concat([no_phone, instagram_messenger]).drop_duplicates()

# Ordenar por tipo y asesora
no_phone_full = no_phone_full.sort_values(['lead_type', 'Funnel Name'])

# Columnas relevantes para el reporte
cols = ['Lead', 'Name', 'Funnel Name', 'Stage Name', 'Channel', 'lead_type', 'Value', 'Created At', 'Last Message']
export = no_phone_full[cols].copy()
export.columns = ['Identificador', 'Nombre', 'Asesora', 'Etapa Leadsales', 'Canal', 'Tipo', 'Presupuesto', 'Fecha Creacion', 'Ultimo Mensaje']

output_path = r'C:\Users\Salo\Downloads\contactos_no_telefono.xlsx'
export.to_excel(output_path, index=False)

print(f"Total contactos sin telefono real: {len(export)}")
print()
print("=== DESGLOSE ===")
print(export['Tipo'].value_counts().to_string())
print()
print("=== POR ASESORA ===")
print(export['Asesora'].value_counts().to_string())
print()
print("=== POR CANAL ===")
print(export['Canal'].value_counts().to_string())
print()
print(f"Archivo guardado en: {output_path}")
