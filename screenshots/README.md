# Capturas del dashboard de observabilidad (LangSmith)

Capturas reales del proyecto `l10n-qa-orchestrator` en LangSmith, tomadas después de ejecutar la prueba de cinco
solicitudes concurrentes con OpenAI real (`python scripts/load_test.py --label openai --on-waiting approve`).
No se generaron imágenes simuladas ni gráficos manuales.

| Archivo | Qué muestra | Notas |
|---|---|---|
| `01_trazas_5_ejecuciones.png` | Lista de trazas de la corrida. | Cada tarea genera una traza; las que pasan por aprobación humana generan **dos** (antes y después de aprobar), por eso hay más de 5 filas. |
| `02_modelo_y_tokens.png` | Modelo utilizado y tokens por ejecución. | La suma de tokens de las 5 tareas fue 2.889 (coincide con el resumen del script). |
| `03_costo_por_ejecucion.png` | Costo por ejecución **calculado por LangSmith**. | Es el costo informado por la plataforma. El costo estimado por el script quedó «desconocido» (no se configuraron tarifas `OPENAI_PRICE_*`). |
| `04_latencia_p95.png` | Gráfico «Trace latency percentiles over time» del Monitor. | **Limitación:** el Monitor mostró los percentiles **P50 y P99**, no P95. El p95 de la corrida es el que calculó el script (cliente, nearest-rank): duración total 4,73 s; con 5 muestras equivale al máximo. |
| `05_traza_individual.png` | Una traza abierta con los agentes y etapas ejecutados. | Se ven `supervisor`, `research` (con `inspect_localization_files` y `search_l10n_qa_rules`), `analysis`, `validation` y `llm_report`, con la duración de cada uno. |

## Cómo reproducirlas
1. Configure `.env` (sin comentarios en las líneas de valores) con `LLM_PROVIDER=openai`, `OPENAI_API_KEY`,
   `OPENAI_MODEL`, `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` y `LANGSMITH_PROJECT=l10n-qa-orchestrator`.
2. `docker compose up --build` y verifique en `GET /health`: `"llm_provider": "openai"`, `"llm_configured": true`,
   `"langsmith_tracing": true`. Si cambia el `.env`, recree los contenedores (`docker compose up -d --force-recreate`).
3. `python scripts/load_test.py --label openai --on-waiting approve`; el resumen debe indicar llamadas reales al LLM.
4. En smith.langchain.com, proyecto `l10n-qa-orchestrator`: lista de trazas (capturas 1–3), pestaña **Monitor**
   (captura 4) y una traza abierta (captura 5). Cada traza lleva la etiqueta `job:<job_id>` y `job_id` / `thread_id`
   en sus metadatos.
