"""Create (if needed) and install every query in graph/queries.gsql on the Savanna workspace, timing the compile."""
import os, re, sys, time
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
from pyTigerGraph import TigerGraphConnection

c = TigerGraphConnection(host=os.environ["TG_HOST"], graphname="GraphSleuth", gsqlSecret=os.environ["TG_SECRET"], tgCloud=True)
c.getToken(os.environ["TG_SECRET"])
t0 = time.time()
out = c.gsql((ROOT / "graph" / "queries.gsql").read_text())
created = re.findall(r"Successfully created queries: \[(\w+)\]", out)
print(f"created {len(created)} queries in {time.time() - t0:.0f}s", flush=True)
t1 = time.time()
out = c.gsql("USE GRAPH GraphSleuth\nINSTALL QUERY ALL")
print(f"INSTALL QUERY ALL finished in {time.time() - t1:.0f}s", flush=True)
print(out[-1500:])
