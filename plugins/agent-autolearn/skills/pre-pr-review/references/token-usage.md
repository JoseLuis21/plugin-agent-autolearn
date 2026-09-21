# Medicion local de tokens

El plugin instala `hooks/hooks.json`: command hooks **SubagentStop** (revisores y agregador) y
**Stop** (estimacion del orquestador), sin llamadas al modelo.
Procesa solo los revisores del plugin marcados con esta linea en su prompt inicial o de reanudacion:

```text
PRE_PR_USAGE: {"run_dir":"/repo/.pre-pr-review/corrida","reviewer":"code-reviewer"}
```

El directorio debe existir, estar bajo `.pre-pr-review/` del repo y tener un `run.json` valido.
Los `--run-dir` fuera de ese directorio no se capturan automaticamente. No adivina la corrida por
fecha ni examina todos los historiales de la cuenta. Lee solo `agent_transcript_path` entregado
por el hook. Guarda contadores/modelos, nunca prompts, codigo, respuestas ni rutas de transcripts.

## Que se cuenta

- `input_tokens`: entrada nueva registrada por peticion.
- `cache_creation_input_tokens`: entrada escrita en cache.
- `cache_read_input_tokens`: entrada leida de cache.
- `output_tokens`: salida **registrada**, que puede ser provisional segun la version/runtime.
- Peticiones unicas por id y modelos observados; bloques de herramientas/stream duplicados no suman.

Se conserva el maximo observado de cada contador para el mismo id. No se suman mensajes result,
tool_result, historiales anteriores a la marca ni otras corridas de un agente reanudado.
Cada agente real tiene un archivo independiente en `usage/`: repetir el hook actualiza ese archivo;
un nuevo intento con otro agent_id suma. Los hooks pueden escribir en paralelo sin compartir archivo.

## Orquestador (estimado)

Al terminar cada turno, el hook Stop lee el transcript principal solo si menciona
`prepare_review.py`. La ventana de una corrida va desde la llamada a `prepare_review.py` hasta el
final del turno que ejecuto `summarize`, y se vincula al `run_dir` de la linea PRE_PR_USAGE que
el propio orquestador puso al lanzar agentes. Texto del usuario o resultados de herramientas no
pueden abrir, vincular ni mover una ventana. Mensajes de subagentes (sidechain) se excluyen.
Las ventanas de una corrida reanudada se unen por id de peticion.

Es una **estimacion** y se reporta aparte (`orchestrator`, `orchestrator_tokens`,
`total_with_orchestrator`): incluye la lectura de cache del contexto previo de la conversacion,
y una corrida `unchanged` o interrumpida antes de lanzar agentes no se atribuye. `partial` indica
ventana sin cerrar o contadores incompletos. `observed_tokens` sigue siendo solo revisores y
agregador. El resumen se regenera solo tras el turno; no hay que esperarlo ni recalcularlo.

## Perfil de turnos y conversacion larga

Cada registro de `usage/` incluye `peak_context`, `tool_calls`, `single_tool_turns` y `parallel_turns`:
contadores del transcript, sin contenido. Un agente con muchos turnos de una sola herramienta y contexto
alto es el primer candidato a optimizar. Registros de versiones anteriores no traen perfil: N/D, no cero.
El resumen marca `orchestrator.long_conversation` cuando el orquestador releyo mas de ~100k tokens por
peticion; `prepare_review.py` avisa antes (`conversation_context`) leyendo solo el `usage` de la ultima
peticion de la sesion indicada por `CLAUDE_CODE_SESSION_ID`. Si no puede medir, devuelve null y sigue.
`review_usage.py finish --run-dir DIR` equivale a `summarize` + `review_sync enqueue` + push en segundo
plano y devuelve el paquete de entrega; cierra la ventana del orquestador igual que `summarize`.

## Consultar una corrida

Despues de terminar todos los revisores y el agregador:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_usage.py" summarize --run-dir "$RUN_DIR"
```

Produce `usage-summary.json` para comparar corridas y `usage-summary.md` con ranking por tokens,
modelos en JSON, desglose de cache y subtotal observado. Guarda rama, pasada y modo full/incremental.
Incluye `plugin_version`, `tree`/`since_tree`, el resumen de hallazgos de `clasificacion.json` y
`searches`: consultas distintas y repetidas (misma consulta y alcance, normalizados, registrados por
mas de un revisor). Es un diagnostico posterior: ningun revisor espera ni omite evidencia por el.

Una copia identica, solo con contadores y sin rutas locales, se escribe en
`pr-reviews/<rama>/usage/p<pasada>-<corrida>.json` para viajar con la rama (`--no-share` la evita).
Un archivo por corrida: no genera conflictos entre desarrolladores ni entra en el snapshot.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_usage.py" compare <corrida|resumen A> <corrida|resumen B>
```

Compara tokens por revisor, orquestador y hallazgos entre dos resumenes (locales o compartidos) e
indica la version del plugin de cada uno. Solo atribuye diferencias al plugin si `tree` y ventana
coinciden; para medir calidad entre versiones usa las evaluaciones de `evals/README.md`.

Con sincronizacion activa, el hook Stop reenvia en segundo plano (`review_sync.py push`) tras
actualizar el orquestador, y el diagnostico de coste (motivo del modo, patch por archivo, busquedas por
revisor) viaja junto al resultado. Solo contadores y rutas; nunca contenido del patch.

Son artifacts locales excluidos del snapshot: no disparan otra revision. El informe de codigo y
el ledger no se modifican. Se pueden abrir los resúmenes de distintas pasadas para comparar costes.

## Limites de la medicion

**No es la factura ni el consumo total de la conversacion.** El subtotal excluye el orquestador
(estimado aparte), agentes anidados,
peticiones internas ausentes del transcript y cualquier intento que no alcance el hook. No calcula
USD: cache, modelo y proveedor tienen tarifas diferentes. Un token de cache no cuesta lo mismo que
uno de salida. `recorded` significa contadores disponibles para los agentes esperados, no cobertura
de toda la sesion. `partial`/N/D identifica ausencia o registros incompletos; nunca equivale a cero.
Si cambia el formato del transcript o faltan contadores, la medicion se declara parcial.

No uses `total_tokens` de la notificacion final como suma: puede ser solo la ultima peticion.
No pidas a un revisor estimar su propio uso ni uses el tamaño en caracteres del prompt. La salida
por paso puede ser provisional; una factura exacta requiere telemetria de API/OTel o resultados
acumulados del runtime con semantica comprobada. Este primer paso mide el trabajo observado sin
cambiar las llamadas ni la seleccion de revisores.

Si el hook falla, emite diagnostico local y retorna 0: no bloquea el cierre, no reintenta la revision
ni cambia su veredicto. Si los hooks estan deshabilitados, el resumen muestra los faltantes.

Fuentes del contrato del runtime:
- [SubagentStop: ruta del transcript propio](https://code.claude.com/docs/en/hooks#subagentstop).
- [Alcance de total_tokens al completar un subagente](https://code.claude.com/docs/en/monitoring-usage#subagent-completed-event).
- [Uso por paso, deduplicacion y limites de salida](https://code.claude.com/docs/en/agent-sdk/cost-tracking#track-per-step-usage).
