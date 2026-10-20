#!/usr/bin/env python3
"""Demostración reproducible del flujo completo con aprobación humana.

    python scripts/demo_hitl.py --decision approve     # o: --decision reject

Pasos: crea la tarea → espera WAITING_APPROVAL → muestra qué se pide aprobar → envía la decisión →
espera el estado final → muestra el resultado y si la operación crítica (importación SIMULADA) se ejecutó.
Requiere la API y el worker en marcha (docker compose up, o uvicorn + python -m app.worker).
"""
import argparse
import json
import os
import sys
import time

import httpx


def esperar(cliente, job_id, estados, timeout=120):
    fin = time.time() + timeout
    while time.time() < fin:
        t = cliente.get(f"/tasks/{job_id}").json()
        if t["status"] in estados:
            return t
        time.sleep(0.5)
    sys.exit(f"Timeout esperando {estados}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.getenv("API_URL", "http://localhost:8000"))
    ap.add_argument("--decision", choices=["approve", "reject"], default="approve")
    ap.add_argument("--token", default=os.getenv("APPROVAL_TOKEN", ""))
    ap.add_argument("--origen", default="source/clean.en.json")
    ap.add_argument("--destino", default="targets/clean.es.json")
    a = ap.parse_args()
    with httpx.Client(base_url=a.base_url, timeout=30) as c:
        r = c.post("/tasks", json={"request": "QA estructural previo a la importación al CMS",
                                   "source_paths": [a.origen], "target_paths": [a.destino], "require_approval": True})
        print("1) POST /tasks →", r.status_code, r.json())
        job = r.json()["job_id"]
        t = esperar(c, job, {"WAITING_APPROVAL", "DONE", "FAILED", "REJECTED"})
        print("2) Estado:", t["status"])
        if t["status"] != "WAITING_APPROVAL":
            print("   La tarea terminó sin pedir aprobación:", json.dumps(t.get("result", {}).get("importacion"), ensure_ascii=False),
                  t.get("error"))
            return 0
        print("   Se pide aprobar:", json.dumps(t["approval_request"]["paquete"], ensure_ascii=False)[:400])
        cab = {"X-Approval-Token": a.token} if a.token else {}
        r = c.post(f"/tasks/{job}/approve", headers=cab, json={"decision": a.decision, "approver": "demo", "comment": "Demostración"})
        print("3) POST /tasks/{job_id}/approve →", r.status_code, r.json())
        r2 = c.post(f"/tasks/{job}/approve", headers=cab, json={"decision": a.decision, "approver": "demo"})
        print("   Segunda aprobación (debe rechazarse) →", r2.status_code, r2.json())
        t = esperar(c, job, {"DONE", "FAILED", "REJECTED"})
        print("4) Estado final:", t["status"])
        print("   Importación:", json.dumps(t["result"]["importacion"], ensure_ascii=False, indent=2))
        print("   Métricas:", json.dumps({k: t["metrics"].get(k) for k in ("queue_wait_s", "execution_s", "total_s")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
