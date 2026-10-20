# Capturas del dashboard de observabilidad (LangSmith)

Son capturas reales del proyecto `l10n-qa-orchestrator` en LangSmith, que saqué después de correr la prueba de las
cinco solicitudes concurrentes con OpenAI real (`python scripts/load_test.py --label openai --on-waiting approve`).
No hay imágenes simuladas ni gráficos armados a mano.

| Archivo | Qué muestra |
|---|---|
| `01_trazas_latencia_tokens_costo.png` | Lista de trazas («Traces», último día): nombre, latencia, **tokens** y **costo por ejecución** calculado por LangSmith. |
| `02_trazas_tags_metadata.png` | La misma lista, desplazada a la derecha: la etiqueta `job:<job_id>` y los metadatos de cada traza (para relacionarla con la tarea). |
| `03_latencia_percentiles_monitor.png` | Gráfico «Trace latency percentiles over time» del Monitor. |
| `04_traza_individual_agentes.png` | Una traza abierta (vista de hilo): nodos `supervisor`, `research`, `analysis`, `validation`, `llm_report` (llamada a `gpt-4o-mini`, 436 tokens) y, en el segundo turno, `approval_gate` y `execute_import`. |

## Cosas que conviene aclarar

- **Hay 11 trazas y no 5.** Son: la prueba de carga (5 tareas; las 3 sin errores pasan por la aprobación humana y por
  eso generan una segunda traza al reanudarse, las 2 con errores generan una sola), una ejecución del demo
  `scripts/demo_hitl.py` (`job:0fd280…`, 459 tokens) y **una traza en rojo** (`job:31c810…`, `OpenAIRateLimitError`) de
  un intento anterior, cuando todavía no había cargado crédito en OpenAI. La dejé porque es una ejecución real y
  muestra cómo se maneja el error (la tarea quedó en `FAILED` con el mensaje del proveedor).
- **Tokens de la prueba de carga:** 436, 436, 450, 755 y 812. Suman **2.889**, igual que el resumen del script.
- **Costo por ejecución:** el que informa LangSmith (entre $0,00011 y $0,00018 por tarea, unos $0,00069 las cinco). El
  costo *estimado* por el script quedó «desconocido» porque no configuré las tarifas `OPENAI_PRICE_*`.
- **Latencia p95:** el Monitor me mostró los percentiles **P50 y P99**, **no P95**. El p95 de la corrida es el que
  calcula `scripts/load_test.py` (del lado del cliente, nearest-rank): duración total **4,73 s**, que con 5 muestras
  es el máximo. Además, las reanudaciones tras la aprobación duran ~0,01–0,02 s y hacen bajar los percentiles del
  gráfico: cada tarea con aprobación aparece como dos trazas de duraciones muy distintas.

## Cómo reproducirlas

1. Configurar el `.env` (sin comentarios en las líneas de valores) con `LLM_PROVIDER=openai`, `OPENAI_API_KEY`,
   `OPENAI_MODEL`, `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` y `LANGSMITH_PROJECT=l10n-qa-orchestrator`.
2. `docker compose up --build` y revisar en `GET /health`: `"llm_provider": "openai"`, `"llm_configured": true`,
   `"langsmith_tracing": true`. Si se cambia el `.env`, hay que recrear los contenedores
   (`docker compose up -d --force-recreate`).
3. `python scripts/load_test.py --label openai --on-waiting approve`; el resumen tiene que indicar llamadas reales al LLM.
4. En smith.langchain.com, proyecto `l10n-qa-orchestrator`: lista de trazas, pestaña **Monitor** y una traza abierta.
