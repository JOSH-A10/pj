import ee
from dotenv import load_dotenv
import os

load_dotenv()
ee.Initialize(project=os.getenv('GEE_PROJECT'))

tasks = ee.data.getTaskList()[:10]
gedi_tasks = [t for t in tasks if 'GEDI' in str(t.get('description', ''))]

print(f"Found {len(gedi_tasks)} GEDI tasks:\n")
for t in gedi_tasks:
    print(f"{t['description']}: {t['state']}")
