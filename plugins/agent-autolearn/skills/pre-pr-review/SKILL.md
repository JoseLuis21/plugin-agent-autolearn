---
name: pre-pr-review
description: Pregunta que rama existente revisar si el usuario no la indico y revisala siempre contra development, con revisores por delta y ledger persistente. Reutiliza su cobertura para continuar incremental; --full solo por peticion explicita. Usar al pedir revision pre-PR o invocar /pre-pr-review.
---

# Pre-PR Review

Preparas contexto, lanzas revisores y agregas. No arreglas codigo durante la revision.
La memoria es el **contenido revisado con cobertura completa**, no solo el commit HEAD.
Las decisiones finales del agregador y las verificaciones explicitas actualizan el ledger.

## 1. Preparar una vez

**Primero identifica la rama del PR.** Si el usuario no la indico en el mensaje, argumentos o
contexto de esta revision, pregunta: **«¿Que rama existente quieres que revise contra development?»**
Espera su respuesta antes de preparar o lanzar revisores. No asumas que es la rama actualmente
abierta. Si ya la indico, reutiliza esa respuesta sin volver a preguntarla en cada pasada.

La rama ya existe: no crees otra rama de trabajo para revisarla. Comprueba que existe y usa su
checkout existente. Si necesitas cambiar a ella, hazlo solo con el arbol limpio; con cambios
locales en otra rama, pide al usuario que deje disponible la rama indicada, sin stash automatico,
descarte ni traslado de cambios. Si la rama no se encuentra, pide corregir el nombre o hacerla
disponible; no la crees desde development. Confirma que el checkout corresponde a la rama indicada
antes de ejecutar el helper. `$ROOT` debe apuntar a ese checkout.

La base es **siempre development**, aunque el PR apunte a main/production o `origin/HEAD`
apunte a otra rama. Nunca uses main, master, prod ni production como base de revision.
Actualiza `origin/development` con `git fetch origin development` cuando haya remoto y acceso;
si falla, informa de la limitacion. El helper prefiere origin/development y usa development local
solo si no existe el ref remoto. Si no existe ninguna, detente y explica que falta development.

Resuelve `$ARGUMENTS`: nombre de la rama existente a revisar, `--full` o `--verify-pending`.
`--recheck-rules` y `--keep-rules-coverage` solo responden a la pregunta de reglas de mas abajo.
El nombre recibido es la **rama de origen del PR**, nunca el argumento `--base` del helper.
Una base explicita solo puede ser development.
Un numero de PR identifica la rama a revisar, **no cambia la base**. Si recibes rutas, aclara que
este protocolo cubre el delta de la rama completa, no una seleccion parcial.
No añadas `--full` por estar cerca de abrir el PR ni porque exista un informe previo:
usalo solo cuando el usuario pida expresamente una revision completa.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py" --repo "$ROOT"
# Añade --full para validar toda la rama, o --verify-pending para revalidar sin delta.
```

El helper fija development y decide completo/incremental a partir de la cobertura guardada. Lee su salida y conserva
`run_dir`. No copies todos los artifacts al prompt. `run.json` contiene las rutas y decisiones.
El protocolo de CLI, schemas y recuperacion esta en `references/review-state.md`: leelo al preparar
la primera corrida o resolver un fallo del helper.

El helper crea un indice temporal aislado, captura staged + unstaged + untracked no ignorados,
y calcula **un solo diff entre arboles**. No modifica el indice real ni crea commits o stashes.
Excluye artefactos/generados; los lockfiles permanecen en el snapshot y se asignan a seguridad
para lectura a demanda. Nunca copies snapshots de codigo al directorio versionado de informes.

- `status: unchanged`: no lances agentes. Informa del numero de pendientes; no fuerces otro full.
- `mode: completo`: primera pasada, `--full`, ledger legado, ancla ausente, base cambiada o rebase.
  La numeracion sigue `max(pasadas.n)+1` y el historial se conserva.
- `mode: incremental`: `new.patch` compara la ultima cobertura completa con el contenido actual.
  Commitear contenido ya revisado no vuelve a abrir esa ventana.
- Si falta el snapshot local, el helper intenta reconstruirlo desde commits disponibles y exige
  el mismo hash. `recovered_from_commit` identifica el commit usado: informa de la recuperacion,
  sin lanzar full ni pedir confirmacion por cambiar de equipo. No necesita IA ni acceso a red.
  Para compartir continuidad, subir codigo y pr-reviews/ en la misma rama; .pre-pr-review/ sigue
  local. Un snapshot no recuperable sigue el flujo de cobertura invalida del punto siguiente.
- Si la rama ya tuvo revision, reutiliza su ledger y snapshot. No reinicies el historial ni
  fuerces un full. Si el helper indica completo por cobertura invalida (rebase, base cambiada,
  ledger antiguo o snapshot perdido), explica el motivo y pregunta antes de lanzar otra revision
  completa, salvo que el usuario ya haya pedido `--full`. Un informe previo sin estado reutilizable
  tampoco autoriza a repetir el full silenciosamente.
- `resumed: true`: la corrida anterior de este mismo contenido quedo a medias y sus resultados
  validos se conservan. Usa ese `run_dir`, lanza solo `pending_reviewers` (check_owner primero si
  esta entre ellos) y, si la lista esta vacia, pasa al agregador. No repitas `completed_reviewers`.
  Si el codigo, el ledger o las reglas cambiaron, el helper prepara una corrida nueva por si solo.
- `rules_changed` no vacio: las reglas de esos revisores cambiaron desde que cubrieron la rama y
  el incremental solo aplicara las nuevas al delta. Explicalo con los archivos indicados y pregunta:
  repetir **solo esos revisores** sobre la rama completa (`--recheck-rules`) o conservar la cobertura
  (`--keep-rules-coverage`). Vuelve a ejecutar el helper con la opcion elegida; nunca la elijas tu ni
  la conviertas en `--full`. Los revisores sin superficie en la rama se renuevan solos, sin preguntar.
- `cleanup` con `runs_removed`/`refs_removed` mayor que cero: mencionalo en una linea al entregar.
  Politica y desactivacion en `references/review-state.md`; la cobertura vigente nunca se borra.
- Un error de preparacion se explica y se corrige antes de lanzar. Nunca sustituyas un ledger
  ilegible por uno vacio. Mas de 150 archivos o 5000 lineas: informa del tamaño sin recortar.

Los artifacts locales quedan en `.pre-pr-review/<corrida>/`; no borres una corrida al iniciar otra.
Si hace falta ignorarlos, añade `.pre-pr-review/` a `.gitignore` **antes** de preparar y verifica
la regla y el salto de linea final. Nunca ignores `pr-reviews/`.

## 2. Lanzar solo los revisores asignados

`run.json.expected_reviewers` es la lista autoritativa; `skipped_reviewers` explica las omisiones.
El helper decide con el **delta actual**, no con todos los archivos historicos de la rama:

| Revisor | Cuando corre |
|---|---|
| security | Cualquier delta no excluido, incluidos docs, tests, config, secretos y lockfiles; o pendientes propios |
| code | Codigo/config o superficie ambigua; o pendientes propios. Incluye limites, contratos y tests cuando no hay especialista |
| edge-case, regression, test | Validacion completa con codigo, delta grande/señales de riesgo, tests modificados cuando aplica, o pendientes propios |
| convention | El escaner unico produjo candidatos, o tiene pendientes |
| nextjs-architecture | Delta JS/TS en Next.js, o pendientes propios. Recibe el grafo de frontera cliente/servidor |
| go-architecture | Delta Go en estructura hexagonal, o pendientes propios |

El helper resuelve el grafo de imports una vez por corrida Next.js y deja en
`_client-server-raw.json` las cadenas donde un Client Component alcanza un modulo de servidor
(`server-only`, `next/headers`, builtin de Node). Eso rompe el build, no es una opinion: el
revisor las confirma archivo a archivo antes de reportarlas como BLOCKER.

El helper escanea convenciones una vez. No lances al confirmador para un escaneo vacio sin pendientes.
Cada hallazgo activo se entrega a su dueño una vez cuando hay delta; con `--full` o
`--verify-pending` tambien sin cambios. Los heredados sin dueño se asignan a code.
Una omision justificada no es cobertura incompleta. Un resultado asignado que falta o es invalido, si.

Lanza primero `run.check_owner` (test, o code/security si no fue asignado). Cumple su revision
normal y publica los hechos de build/tests en `shared-checks.json` segun
`references/shared-checks.md`; pasa esta referencia en su prompt. No añade un agente ni otra
auditoria. Al terminar, valida lo publicado sin ejecutar nada ni gastar tokens:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/shared_checks.py" --run-dir "$RUN_DIR"
```

El resultado queda en `shared-checks.json.validation`; con exit 1 los hechos son contradictorios
o de otro snapshot y nadie los reutiliza. No relances al dueño ni cambies la cobertura por ello.
Despues lanza los demas asignados **en paralelo**, con esos resultados disponibles.
Si el dueño falla, no lo relances automaticamente: conserva cobertura incompleta y permite a los
demas revisar; las comprobaciones ausentes no se consideran exitosas. Esta secuencia intercambia
algo de paralelismo por evitar ejecuciones duplicadas, sin reducir revisores, modelos ni checklists.
Si check_owner es null, lanza directamente los asignados en paralelo; no añadas un revisor para
ejecutar checks sin superficie aplicable.
Prompt comun (sustituye rutas y nombre; no pegues el patch):

```text
Revisa esta corrida pre-PR como <reviewer>.
PRE_PR_USAGE: {"run_dir":"<ruta absoluta de run_dir>","reviewer":"<reviewer>"}
Repo: <run.repo>. RUN_DIR: <run_dir>.
Lee brief.md, repo-context.json, tu pending/<reviewer>.json y findings-contract.md.
Lee references/shared-checks.md y shared-checks.json: reutiliza solo hechos aplicables al mismo
snapshot, comando, entorno y alcance. Un build verde no prueba correctitud ni seguridad.
Cada especialista mantiene su analisis independiente; no leas los findings de otros para sustituirlo.
Lee new.patch una vez; full.patch solo para resolver una duda concreta.
Excepcion convention: lee _conventions-raw.json, sin leer ni volver a escanear el patch.
Excepcion nextjs-architecture: abre _client-server-raw.json antes del patch y confirma cada
cadena con en_delta true leyendo sus archivos; confirmada es BLOCKER (rompe el build).
Seguridad: abre dependencies.json y revisa tambien los lockfiles cambiados a demanda.
<solo si run.discarded_context > 0> Lee discarded.json: descartes previos en estos archivos.
No es un filtro. Si el motivo del descarte sigue valiendo, no lo reportes otra vez; si el codigo
o una prueba nueva lo refuta, reportalo con new_evidence explicando que cambio.
<solo si eres de run.full_window_reviewers> Tus reglas cambiaron: tu ventana es full.patch.
Revalida CADA huella asignada con status open/closed/discarded y evidencia concreta;
la ausencia de un hallazgo en el diff no es prueba de cierre. Conserva el dueño en reviewer.
Para un hunk, lee su funcion; amplia solo cuando el juicio lo necesita.
Consulta searches.json y busca la consulta/simbolo en searches/*.json antes de buscar en el repo.
Los fragmentos se publican durante la revision, no solo al final; cada revisor escribe el suyo
atomicamente (temporal + rename). Reutiliza ubicaciones, no conclusiones: confirma el codigo relevante.
Una consulta simultanea puede repetirse; no esperes ni omitas evidencia por ahorrar una llamada.
Completa ubicaciones dentro de la ventana. Sigue callers afectados con una consulta justificada;
fuera de alcance, registra una muestra para seguimiento, sin censos del repo.
Escribe <reviewer>.json segun el contrato. No edites codigo ni el ledger.
Responde con ruta y conteo, sin repetir patch/JSON.
```

Las leyes siguen siendo las skills del plugin: seguridad→owasp-security, edge→resilience,
regression→safe-refactor, test→testing, arquitectura→development-<stack>.
Lee la seccion relevante para el cambio y la calibracion de `references/`. La ley propia del repo
manda cuando existe. Un hallazgo con regla escrita la cita. No cambies modelos para ocultar coste.
Para code indica cuales checklists le delega `skipped_reviewers`; los especialistas evitan duplicarlos.

Si una duda requiere ampliar cobertura, registra el revisor adicional y sus responsabilidades en
`run.json` **antes de lanzarlo**, con `assignments[reviewer]: []` si no tiene pendientes, y quitalo
de `skipped_reviewers`. No reduzcas la lista para convertir un fallo en una omision.

## 3. Curar y finalizar

Lanza un `review-aggregator` cuando terminen los asignados, con RUN_DIR, repo, ruta `run.report`
y `${CLAUDE_PLUGIN_ROOT}`. Incluye tambien la linea de medicion
`PRE_PR_USAGE: {"run_dir":"<ruta absoluta de run_dir>","reviewer":"review-aggregator"}`.
Lee sus instrucciones; no le pegues otra copia de los resultados.
El agregador es el unico responsable de validar identidad/schema/cobertura, curar evidencias,
severidad, duplicados y descartes, escribir `curated.json`, ejecutar `ledger.py` una vez despues
de validar y escribir `run.report`. El orquestador solo lee los artifacts finales; no vuelve a
finalizar ni reescribe el informe.

El helper persiste evidencia, fix, decision externa, aceptaciones, aliases y todo el historial.
Un cierre requiere verificacion de esa huella por su revisor asignado; tocar el archivo no basta.
La referencia al snapshot avanza solo tras validar toda la corrida. Un fallo deja la cobertura
previa y produce `REVISION INCOMPLETA`; conserva artifacts para completar o diagnosticar.
No relances silenciosamente ni declares GO. Los conteos salen de `clasificacion.json` final.

`accepted.json` solo cambia por instruccion humana expresa, con motivo y autor del usuario.
Su techo de severidad se aplica tambien a hallazgos arrastrados. `decision_externa` se conserva
entre pasadas y sale del conteo accionable, pero sigue visible en el informe.

## 4. Entregar

Una vez que el agregador haya terminado (tambien si fallo), ejecuta:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_usage.py" summarize --run-dir "$RUN_DIR"
```

Despues, sin esperar y sin leer su salida, encola y envia la corrida a Agent Autolearn. Ambos
comandos no hacen nada si no hay un perfil activo, y un fallo no cambia el veredicto ni el informe:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_sync.py" enqueue "$RUN_DIR" --quiet >/dev/null 2>&1 || true
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/review_sync.py" push --repo "$RUN_DIR" --quiet >/dev/null 2>&1 &
```

El hook SubagentStop registra contadores por peticion del transcript, deduplicados por id.
Mantiene un archivo por agente real: reintentos nuevos suman; volver a recibir el mismo no duplica.
Incluye la linea PRE_PR_USAGE en cada lanzamiento/reanudacion; serializa el JSON correctamente.
Lee solo la salida compacta del script y enlaza `usage-summary.md` junto al informe. No reescribas
el informe del agregador. Nunca inventes tokens, uses caracteres como medida ni sumes el
`total_tokens` de la ultima peticion como si fuera toda la ejecucion. Si faltan hooks/transcripts,
reporta medicion parcial/N/D: no es cero ni afecta al veredicto de codigo.
El subtotal cubre revisores y agregador, **no el orquestador**: su consumo se estima aparte con
el hook Stop al terminar el turno y actualiza el mismo resumen; no lo calcules ni lo esperes.
Una copia solo con contadores queda junto al ledger (`pr-reviews/<rama>/usage/`) para compartirla
con la rama. Detalles, consultas repetidas y comparacion de corridas: `references/token-usage.md`.

Lee el informe nuevo en `run.report`; de clasificacion.json extrae solo `resumen` con un script.
No cargues todos los historiales para resumir un informe ya curado.
Resume en maximo 8 lineas:
veredicto, conteos, cerrados verificados, abiertos/nuevos, fixes bloqueantes, informe y enlace a tokens.
Si `resumen.review_complete` es true, termina: PR listo, observaciones restantes como seguimiento
no bloqueante. No propongas otra ronda ni una lista de arreglos opcionales como siguiente paso.
No ejecutes ciclos hasta cero hallazgos salvo solicitud expresa. No aceptes/cierres pendientes
automaticamente; si el usuario pide otra revision, revisa su delta normalmente, incluidos bugs nuevos.
Mismo simbolo/archivo tocado no demuestra que un arreglo causo otro bug: usa `REINCIDENTE` neutral;
`INTRODUCIDO_POR` exige evidencia causal antes/despues. No culpes al autor por una heuristica.

Una cobertura completa valida contra development se conserva: las siguientes corridas son
incrementales. Una invalidacion real requiere explicar el motivo y obtener la decision del usuario
antes de repetir el full; `--full` explicito ya expresa esa decision.
No hagas commit, push ni abras el PR. Si hay HIGH/BLOCKER, ofrece corregirlos; solo con autorizacion.
