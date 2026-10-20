#!/usr/bin/env python3
"""Prueba de carga: EXACTAMENTE cinco solicitudes concurrentes a ``POST /tasks``.

Qué mide (cada cosa por separado, sin mezclarlas):
  1. Latencia de encolado: tiempo de la respuesta HTTP de ``POST /tasks`` (medida en el cliente).
  2. Duración total de cada tarea: desde que se envía el POST hasta que la tarea llega a un estado final
     (o a ``WAITING_APPROVAL`` si se eligió ``--on-waiting report``). Incluye la cola y, si se aprueba
     automáticamente, la reanudación.
  3. Tiempo de ejecución en el servidor (``metrics.execution_s``), que excluye esperas de cola y humanas.
  4. p95 del conjunto de duraciones: método *nearest-rank* (el valor ordenado en la posición ⌈0,95·n⌉).
     Con n = 5, el p95 es el valor MÁXIMO; se informa así para no dar una falsa sensación de precisión.

Uso:
  python scripts/load_test.py --base-url http://localhost:8000 --on-waiting approve --label local
Salida: resultados/load_test_<label>_<fecha>.json y .csv

Con ``--on-waiting``: ``approve`` / ``reject`` resuelven automáticamente las tareas que piden aprobación;
``report`` (por defecto) deja de consultar esa tarea y la informa como WAITING_APPROVAL (no queda en un
sondeo infinito).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

N_SOLICITUDES = 5            # fijo por consigna: no es configurable
FINALES = {"DONE", "FAILED", "REJECTED"}

# Cinco casos independientes (3 pares sin errores y 2 con errores), para ejercitar ambos caminos.
CASOS = [
    ("clean-es", "source/clean.en.json", "targets/clean.es.json"),
    ("home-es", "source/home.en.json", "targets/home.es.json"),
    ("clean-es-b", "source/clean.en.json", "targets/clean.es.json"),
    ("home-ar", "source/home.en.json", "targets/home.ar.json"),
    ("clean-es-c", "source/clean.en.json", "targets/clean.es.json"),
]
assert len(CASOS) == N_SOLICITUDES


def percentil_nearest_rank(valores: list[float], p: float) -> float | None:
    if not valores:
        return None
    ordenados = sorted(valores)
    return ordenados[max(1, math.ceil(p / 100 * len(ordenados))) - 1]


async def enviar(cliente: httpx.AsyncClient, nombre: str, origen: str, destino: str, solicitud: str) -> dict:
    cuerpo = {"request": solicitud, "source_paths": [origen], "target_paths": [destino], "require_approval": True}
    t0 = time.perf_counter()
    registro = {"caso": nombre, "origen": origen, "destino": destino, "t_envio": t0, "http_status": None,
                "job_id": None, "thread_id": None, "latencia_encolado_ms": None, "error": None}
    try:
        r = await cliente.post("/tasks", json=cuerpo)
        registro["latencia_encolado_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        registro["http_status"] = r.status_code
        if r.status_code == 202:
            d = r.json()
            registro.update(job_id=d["job_id"], thread_id=d["thread_id"], enqueue_ms_servidor=d.get("enqueue_ms"))
        else:
            registro["error"] = f"HTTP {r.status_code}: {r.text[:300]}"
    except httpx.HTTPError as exc:
        registro["error"] = f"{type(exc).__name__}: {exc}"
    return registro


async def esperar(cliente: httpx.AsyncClient, reg: dict, on_waiting: str, aprobador: str, token: str,
                  timeout: float, intervalo: float) -> None:
    if not reg["job_id"]:
        reg["estado_final"] = "NO_ENCOLADA"
        return
    limite = time.perf_counter() + timeout
    decidida = False
    tarea: dict = {}
    while time.perf_counter() < limite:
        try:
            r = await cliente.get(f"/tasks/{reg['job_id']}")
            tarea = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            reg["error"] = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(intervalo)
            continue
        estado = tarea.get("status")
        if estado in FINALES:
            break
        if estado == "WAITING_APPROVAL":
            if on_waiting == "report":
                break
            if not decidida:
                cab = {"X-Approval-Token": token} if token else {}
                dec = await cliente.post(f"/tasks/{reg['job_id']}/approve", headers=cab, json={
                    "decision": on_waiting, "approver": aprobador, "comment": "Decisión automática de load_test.py"})
                reg["aprobacion_http"] = dec.status_code
                decidida = dec.status_code == 202
                if not decidida:
                    reg["error"] = f"No se pudo resolver la aprobación: HTTP {dec.status_code} {dec.text[:200]}"
                    break
        await asyncio.sleep(intervalo)
    else:
        reg["error"] = f"Timeout de {timeout}s esperando un estado final"
    reg["duracion_total_s"] = round(time.perf_counter() - reg["t_envio"], 4)
    reg["estado_final"] = tarea.get("status", "DESCONOCIDO")
    m = tarea.get("metrics") or {}
    r = tarea.get("result") or {}
    llm = r.get("resumen_llm") or m.get("llm") or {}
    reg.update(
        ejecucion_servidor_s=m.get("execution_s"), espera_en_cola_s=m.get("queue_wait_s"),
        total_servidor_s=m.get("total_s"), intentos=tarea.get("attempts"),
        estado_qa=r.get("estado_general"), hallazgos=len(r.get("hallazgos") or []),
        importacion=(r.get("importacion") or {}).get("estado"), error_tarea=tarea.get("error"),
        llm_proveedor=llm.get("proveedor"), llm_modelo=llm.get("modelo"), llm_llamada_real=llm.get("llamada_real"),
        tokens_entrada=(llm.get("uso") or {}).get("tokens_entrada"), tokens_salida=(llm.get("uso") or {}).get("tokens_salida"),
        tokens_totales=(llm.get("uso") or {}).get("tokens_totales"), costo_usd=(llm.get("costo") or {}).get("usd"),
        costo_origen=(llm.get("costo") or {}).get("origen"),
        etapas=[{"agente": e["agente"], "segundos": e["segundos"]} for e in m.get("stages", [])])


def resumir(registros: list[dict]) -> dict:
    ok = [r for r in registros if r.get("http_status") == 202]
    dur = [r["duracion_total_s"] for r in registros if r.get("duracion_total_s") is not None and r.get("job_id")]
    lat = [r["latencia_encolado_ms"] for r in registros if r.get("latencia_encolado_ms") is not None]
    ejec = [r["ejecucion_servidor_s"] for r in registros if r.get("ejecucion_servidor_s") is not None]
    llamadas_reales = sum(1 for r in registros if r.get("llm_llamada_real") is True)
    return {
        "solicitudes_enviadas": len(registros), "aceptadas_202": len(ok),
        "estados_finales": {e: sum(1 for r in registros if r.get("estado_final") == e)
                            for e in sorted({r.get("estado_final") for r in registros})},
        "tareas_con_error": [r["caso"] for r in registros if r.get("error") or r.get("error_tarea")],
        "latencia_encolado_ms": {"min": min(lat, default=None), "max": max(lat, default=None),
                                 "p95": percentil_nearest_rank(lat, 95)},
        "duracion_total_s": {"min": min(dur, default=None), "max": max(dur, default=None),
                             "p95": percentil_nearest_rank(dur, 95)},
        "ejecucion_servidor_s": {"p95": percentil_nearest_rank(ejec, 95)},
        "metodo_p95": "nearest-rank: valor ordenado en la posición ceil(0,95·n); con n=5 equivale al máximo.",
        "llamadas_reales_a_llm": llamadas_reales,
        "tokens_totales": sum(r.get("tokens_totales") or 0 for r in registros) if llamadas_reales else None,
        "aviso_llm": ("Hubo llamadas reales a un LLM en %d de %d tareas." % (llamadas_reales, len(registros))
                      if llamadas_reales else "Ninguna tarea hizo llamadas reales a un LLM (modo local)."),
    }


async def principal(args) -> int:
    cab = {}
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0, headers=cab) as cliente:
        # Espera a que la API y Redis estén listos (tolera arranques lentos).
        for _ in range(int(args.espera_api / 0.5)):
            try:
                h = (await cliente.get("/health")).json()
                if h.get("redis"):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.5)
        else:
            print("La API o Redis no están disponibles.", file=sys.stderr)
            return 2
        salud = (await cliente.get("/health")).json()
        solicitud = "Compará los JSON de origen con sus traducciones y detectá problemas estructurales."
        t_inicio = datetime.now(timezone.utc)
        # --- las cinco solicitudes, en paralelo ---
        registros = await asyncio.gather(*[enviar(cliente, n, o, d, solicitud) for n, o, d in CASOS])
        registros = list(registros)
        # --- seguimiento de cada tarea hasta un estado final / de espera ---
        await asyncio.gather(*[esperar(cliente, r, args.on_waiting, args.aprobador, args.token, args.timeout,
                                       args.intervalo) for r in registros])
    for r in registros:
        r.pop("t_envio", None)
    resumen = resumir(registros)
    salida = {"etiqueta": args.label, "modo_llm": salud.get("llm_provider"), "inicio_utc": t_inicio.isoformat(),
              "base_url": args.base_url, "on_waiting": args.on_waiting, "salud": salud, "resumen": resumen,
              "tareas": registros}
    carpeta = Path(args.salida)
    carpeta.mkdir(parents=True, exist_ok=True)
    base = carpeta / f"load_test_{args.label}_{t_inicio.strftime('%Y%m%dT%H%M%SZ')}"
    base.with_suffix(".json").write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    columnas = ["caso", "job_id", "thread_id", "http_status", "latencia_encolado_ms", "estado_final", "estado_qa",
                "duracion_total_s", "ejecucion_servidor_s", "espera_en_cola_s", "intentos", "hallazgos",
                "importacion", "llm_proveedor", "llm_modelo", "llm_llamada_real", "tokens_entrada",
                "tokens_salida", "tokens_totales", "costo_usd", "costo_origen", "error", "error_tarea"]
    with open(base.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columnas, extrasaction="ignore")
        w.writeheader()
        w.writerows(registros)

    print(f"\n{'caso':<12}{'HTTP':<6}{'encolado ms':<13}{'estado':<18}{'QA':<8}{'total s':<9}{'ejec. s':<9}{'tokens':<8}")
    for r in registros:
        print(f"{r['caso']:<12}{str(r['http_status']):<6}{str(r['latencia_encolado_ms']):<13}{str(r.get('estado_final')):<18}"
              f"{str(r.get('estado_qa')):<8}{str(r.get('duracion_total_s')):<9}{str(r.get('ejecucion_servidor_s')):<9}"
              f"{str(r.get('tokens_totales')):<8}")
    print("\nResumen:", json.dumps(resumen, ensure_ascii=False, indent=2))
    print(f"\nResultados guardados en {base.with_suffix('.json')} y {base.with_suffix('.csv')}")
    fallos = resumen["aceptadas_202"] != N_SOLICITUDES or resumen["tareas_con_error"]
    esperados = FINALES | ({"WAITING_APPROVAL"} if args.on_waiting == "report" else set())
    fallos = fallos or any(r.get("estado_final") not in esperados for r in registros)
    return 1 if fallos else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.getenv("API_URL", "http://localhost:8000"))
    ap.add_argument("--on-waiting", choices=["report", "approve", "reject"], default="report")
    ap.add_argument("--aprobador", default="load_test")
    ap.add_argument("--token", default=os.getenv("APPROVAL_TOKEN", ""), help="Valor de APPROVAL_TOKEN (si está configurado).")
    ap.add_argument("--timeout", type=float, default=180.0, help="Segundos máximos de espera por tarea.")
    ap.add_argument("--intervalo", type=float, default=0.5, help="Segundos entre consultas de estado.")
    ap.add_argument("--espera-api", type=float, default=30.0, help="Segundos máximos esperando /health.")
    ap.add_argument("--label", default="local", help="Etiqueta del archivo de salida (p. ej. local, openai).")
    ap.add_argument("--salida", default="resultados")
    return asyncio.run(principal(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
