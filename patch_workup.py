s=open("scalp_server.py").read()
assert "workup_api" not in s,"already patched"
import re
m=re.search(r"^app\s*=\s*FastAPI\([^)]*\)", s, re.M)
assert m,"no app = FastAPI(...) found — add the two lines by hand"
s=s[:m.end()]+"\n\nfrom workup_api import router as workup_router\napp.include_router(workup_router)\n"+s[m.end():]
open("scalp_server.py","w").write(s)
print("PATCHED")
