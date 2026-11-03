import os
import sys
from pathlib import Path
import uvicorn
from dotenv import load_dotenv

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
load_dotenv(root/'.env')

if __name__=='__main__':
    uvicorn.run('backend.main:app',host=os.getenv('LISTEN_HOST','127.0.0.1'),port=int(os.getenv('PORT','18970')),proxy_headers=False,access_log=False)
