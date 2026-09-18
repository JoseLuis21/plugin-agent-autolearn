# Protocolo de estado v2

Python 3 + Git; sin dependencias externas. Los scripts viven en `scripts/` del plugin.

## Preparacion

El orquestador pregunta la rama existente si el usuario aun no la indico y selecciona su checkout
antes de invocar el CLI. El helper revisa ese checkout; el nombre de la rama del PR nunca se pasa
como `--base`, que sigue fijada a development. No se crea una rama nueva para cada revision.

```bash
python3 scripts/prepare_review.py --repo /repo --base origin/development
python3 scripts/prepare_review.py --repo /repo --base origin/development --full
python3 scripts/prepare_review.py --repo /repo --base origin/development --verify-pending
```

Opcionales: `--run-dir` (nuevo o vacio), `--ledger` (ruta explicita para migracion/pruebas),
`--no-resume`, `--no-cleanup`, y las decisiones de reglas `--recheck-rules` / `--keep-rules-coverage`.
No se acepta una seleccion parcial de rutas como cobertura de la rama.
La unica base permitida es development: se prefiere refs/remotes/origin/development y se usa
refs/heads/development si no existe la remota. --base admite solo los alias de esas ramas;
cualquier otra base se rechaza. No se usa origin/HEAD ni la rama destino del PR. El orquestador
hace fetch origin development cuando corresponde. Si development no existe, se detiene.

El snapshot representa el contenido efectivo del worktree: tracked, staged, unstaged y untracked
no ignorados; incluye borrados, renombres y archivos ignorados ya en el indice. El indice temporal
se elimina al terminar; el real se importa como datos y nunca se escribe. No crea commits/stashes.
Un indice con conflictos exige resolverlos antes. Submodulos se representan por su gitlink, no
por el contenido sin commitear del submodulo: revisalos como repos separados.

Filtros en `prepare_review.py`: dependencias vendorizadas, builds, generados, mocks/snapshots,
minificados/SVG e instrumental (`.claude`, `.agents`, `.cursor`, `.windsurf`, openspec, pr-reviews,
.pre-pr-review). Afectan tambien al arbol base: una reversion neta no deja hunks ficticios.
Los lockfiles **si** afectan el snapshot/delta/routing; no van en el patch compartido.
Seguridad consulta `git diff <since_tree> <tree> -- <lockfile>`; no asumas que repiten el manifiesto.

Cada corrida tiene un directorio unico, con:

- `run.json`: version 2, repo, branch, base, merge_base, head, tree, base_tree, since_tree,
  mode (`completo`/`incremental`), reason, pass_n, status, delta_files, report, ledger,
  ledger_digest, snapshot_ref, expected_reviewers, skipped_reviewers, reviewer_reasons y assignments.
- `repo-context.json`: inventario unico de leyes, manifiestos y muestras de tests.
- `brief.md`: resumen pequeño; `new.patch`: ventana; `full.patch`: contexto opcional en incremental.
- `pending/<reviewer>.json`: hallazgos activos asignados con evidencia/fix/estado previos.
- `_conventions-raw.json`: unico escaneo; `dependencies.json`: lockfiles y arboles a comparar.
- `_client-server-raw.json`: grafo de imports de Next.js; cadenas Client Component → modulo
  de servidor, con `chain`, `marker` y `en_delta`. Candidatos para nextjs-architecture, no hallazgos.
- `searches.json`: indice de consultas reutilizables. Cada agente escribe consultas propias en
  `searches/<reviewer>.json` (crear directorio), con query, scope, results y reason.
- `shared-checks.json`: hechos de build/tests de `run.check_owner`, anclados a tree/head.
  El dueño revisa primero y los demas reutilizan hechos segun `shared-checks.md`; pending/vacio
  no significa exito. No modifica expected_reviewers, assignments ni la cobertura exigida.
- `discarded.json` (solo si aplica): descartes previos cuyo archivo esta en la ventana, con motivo,
  pasada y `file_changed_since_discard`. Contexto, no asignacion ni filtro.
- Resultados `<reviewer>.json`, `curated.json` y `clasificacion.json`: se conservan para auditoria/resume.
- `run.json` incluye ademas `plugin_version`, `rules`, `rules_changed`, `rules_accepted`,
  `full_window_reviewers` y `discarded_context`.

### Reanudar una corrida interrumpida

Sin `--run-dir` ni `--no-resume`, el helper busca una corrida `prepared` cuya identidad sea
exactamente la de esta preparacion: repo, rama, base, merge-base, HEAD, tree, ventana, modo, ledger
y su digest, revisores, asignaciones, check_owner, formato de snapshot y reglas. Si existe, no crea
otra: devuelve ese `run_dir` con `resumed: true`, `completed_reviewers` (su JSON pasa la misma
validacion que usa `ledger.py`) y `pending_reviewers`. Entre varias, gana la que tiene mas
resultados validos. Cualquier diferencia prepara una corrida nueva; nunca se mezclan artifacts de
snapshots distintos. La medicion de tokens sigue sumando en el mismo directorio.

### Cambios en las reglas de revision

`run.rules` guarda el digest de los archivos con los que juzga cada revisor: su `agents/<x>.md`,
`findings-contract.md`, `findings-rules.md` y su ley (`owasp.md` y owasp-security, resilience,
safe-refactor, testing, la calibracion y skill de cada arquitectura, `stacks.md` y los dos
escaneres). `coverage.rules` registra con que reglas quedo cubierta la rama por cada revisor.

En incremental, un revisor con reglas distintas y superficie en la rama aparece en `rules_changed`
con los archivos que cambiaron. Es un aviso: la corrida sigue siendo incremental y valida. La
cobertura de ese revisor se renueva solo tras un modo completo, tras `--recheck-rules` (ese revisor
entra en `full_window_reviewers` y revisa `full.patch`; los demas siguen en el delta) o tras
`--keep-rules-coverage` (decision del usuario, registrada en `pasadas[].rules_accepted`). Revisar
solo el delta con reglas nuevas no la renueva. Revisores sin superficie en la rama se renuevan sin
preguntar. Un ledger anterior sin `coverage.rules` adopta las actuales sin avisos ni full.

`expected_reviewers` contiene identidades exactas, una vez cada una; `assignments` mapea cada una a
una lista de huellas. Todas las huellas activas se asignan una vez cuando hay delta o revalidacion
explicita. Los JSON ajenos a esa lista se ignoran, nunca se suman como cobertura.

El ledger por defecto vive en `pr-reviews/pr-<rama-saneada>-<hash>/ledger.json` para evitar colisiones.
Una carpeta antigua sin hash se reutiliza si su ledger declara exactamente la misma `rama`.
Ledger legado o snapshot no recuperable fuerza completo conservando hallazgos y numeracion. Cambiar la
base real/merge-base o reescribir la historia (HEAD previo deja de ser ancestro) tambien fuerza completo.
Cambiar entre los alias local/remoto de development con el mismo merge-base conserva el
incremental; una cobertura anterior contra main/prod no se reutiliza como cobertura de development.
--full se añade solo por solicitud explicita, nunca automaticamente al abrir el PR.
Si habia revision previa y el helper devuelve completo por cobertura invalida, el orquestador
explica el motivo y pregunta antes de lanzar revisores, salvo que ya se haya solicitado --full.
Preparar artifacts de diagnostico no autoriza a ejecutar otra revision completa.
El mismo contenido dirty o despues de commit da el mismo tree: no hay agentes salvo `--full` o
`--verify-pending`. En la rama base solo queda el cambio local neto.

### Continuidad entre desarrolladores

Comparte la misma rama con codigo y `pr-reviews/` mediante push/pull normales. No publiques
`.pre-pr-review/` ni refs especiales. Si falta coverage.tree, el helper filtra el commit
coverage.head y hasta 50 descendientes recientes en la historia local hacia HEAD. Solo acepta
un arbol cuyo hash sea exactamente coverage.tree y lo conserva bajo una ref local de snapshots.
No descarga commits automaticamente, no modifica el indice/worktree ni reescribe el ledger.
`run.recovered_from_commit` registra el commit que coincidio; la ventana sigue desde coverage.tree.

`snapshot_format`, guardado en run y coverage, identifica algoritmo y filtros. Un cambio conocido
invalida cobertura incluso si el arbol sigue disponible. Ledgers anteriores sin ese campo pueden
recuperarse con los filtros actuales solo por coincidencia exacta del hash. Mantener o cambiar los
filtros nunca autoriza a aceptar un arbol aproximado.

Cambios dirty revisados sirven si despues quedaron en un commit candidato identico. Si no se
compartieron, falta historia por un clone shallow, cambio la base, hubo rebase o no se encuentra
coincidencia dentro del limite, conserva hallazgos y explica la necesidad de full antes de lanzarlo.
No se trasladan transcripts ni consultas entre equipos. Del uso de tokens viaja solo el resumen
de contadores de `pr-reviews/<rama>/usage/` (ver `token-usage.md`).

## Resultados del revisor

El formato de findings sigue `findings-contract.md`. Se requieren tambien scope,
sin_hallazgos_en y **verifications**, incluso vacios:

```json
{
  "reviewer": "code-reviewer",
  "scope": "delta de run.json; checklist de limites/contratos/tests",
  "sin_hallazgos_en": ["contratos: sin cambio de firma"],
  "findings": [],
  "verifications": [
    {"fingerprint": "a1b2c3d4e5", "status": "closed", "evidence": "foo.ts:24 ahora valida null antes de parsear; el camino previo ya no existe", "reviewer": "code-reviewer"}
  ]
}
```

Status de verificacion: `open`, `closed`, `discarded`. Cada asignacion tiene exactamente una;
identidad debe coincidir con el dueño del manifiesto. La evidencia de cierre describe el cambio
comprobado; `discarded` explica el falso positivo. Nunca inferir cierre por ausencia o cobertura
de archivo. Una verificacion open puede conservar el finding previo sin repetir su JSON entero;
si cambia severidad, evidencia, fix o decision externa, emite tambien un finding actualizado.

## Curacion final

El agregador valida los findings y guarda un documento separado. No cambia las fuentes originales:

```json
{
  "schema_version": 2,
  "validated": true,
  "findings": [],
  "verifications": []
}
```

`findings` contiene **cada decision sobre fuentes nuevas**, incluyendo falsos positivos.
Cada entrada lleva el finding completo final (title/category/file/symbol/line/severity/evidence/
why/fix/confidence/decision_externa), mas:

- `status`: `open` o `discarded`; los descartes llevan `discard_reason`.
- `new_evidence`: obligatorio para dejar `open` una huella (o alias) que el ledger tiene descartada.
  Sin el, `ledger.py` rechaza la finalizacion citando el motivo previo: o se repite el descarte o se
  explica que cambio. Nunca se suprime por script. El registro guarda `discard_pass`,
  `discard_blob` y, al reabrirse, `reabierto_tras_descarte`.
- `reviewer`: identidad principal; `detectado_por`: lista de identidades de todas sus fuentes.
- `sources`: huellas de los findings originales que esta decision cubre, sin omitir ni duplicar.
- `aliases`: opcional, huellas originales o historicas que se fusionan por causa raiz. Las sources
  tambien se registran como aliases si difieren de la identidad canonica.
- `introducido_por`: opcional, objeto con fingerprint previo, before, after y causal_evidence.
  No se rellena por compartir funcion o archivo. Si no se prueba, omitirlo.

Huella = primeros 10 hex de SHA1 de `category.strip().lower()|file.strip()|symbol.strip().lower()`.
`line` no participa. `ledger.huella()` es la funcion canonica, util para generar sources sin recalcular
con otro criterio. Si ya existe un alias, se conserva su identidad canonica.

`verifications` copia exactamente las verificaciones asignadas de los revisores (evidencia,
identidad y status). Si el agregador discrepa, pide corregir el resultado al dueño antes de
finalizar; no inventa un cierre. Una decision que actualiza un pendiente debe coincidir en status
con su verificacion. No es necesario repetir los hallazgos historicos sin cambios.

```bash
python3 scripts/ledger.py --run-dir /repo/.pre-pr-review/corrida \
  --curated /repo/.pre-pr-review/corrida/curated.json --summary
# --accepted /otra/ruta.json es opcional; por defecto pr-reviews/accepted.json.
```

Exit 0: ledger actualizado atomicamente y `clasificacion.json` final. Exit 2: estado incompleto,
lista de errores en clasificacion; no se modifica el ledger ni su cobertura. La validacion incluye
identidad/esquema/duplicados de los revisores esperados, fuentes completas, verificacion de cada
pendiente, ledger no cambiado desde preparar, HEAD/base/worktree todavia iguales al snapshot.
Corrige artifacts de una corrida incompleta y repite finalizar; si cambio el codigo o ledger,
prepara otra corrida. Nunca reutilices artifacts viejos con un snapshot nuevo.

Con --summary, stdout contiene solo resumen y rutas; los artifacts mantienen toda la evidencia,
historiales, fuentes y decisiones. Sin el flag se conserva la salida completa compatible.
Los errores se imprimen completos en ambos modos.

`clasificacion.json` incluye `resumen.review_complete`: true solo con cobertura completa y sin
HIGH/BLOCKER accionables. `bloqueantes` y `seguimiento` son listas de huellas accionables, separadas
por ese umbral. Aceptados y decisiones externas quedan fuera de ambas; los externos siguen en
hallazgos para informar su riesgo. Un fallo siempre devuelve review_complete=false.
Completar la revision no cierra ni acepta observaciones y no impide revisar un delta nuevo solicitado.

`clasificacion.json.metricas` se deriva del ledger persistido: pasadas, `rondas_hasta_aprobacion`
(primera pasada con review_complete; null si alguna pasada anterior no registro ese dato),
hallazgos unicos por estado, `falsos_positivos` y su tasa (descartes con motivo),
`duplicados_fusionados`, `reabiertos_tras_descarte` y regresiones abiertas. Cada entrada de
`pasadas` guarda review_complete, veredicto, nuevos, descartados, fusionados y plugin_version.
Son contadores de proceso: no cambian el veredicto. `resumen.shared_checks` informa la validacion
de `shared-checks.json` (`valid`, `pending`, `not_applicable`, `invalid`).

El criterio de cierre, incluido el caso de regresion necesario, se conserva dentro de `fix`.
Una falta de test del mismo arreglo se fusiona bajo category/file/symbol historicos con sources
y aliases, sin crear otra causa. El dueño aporta finding actualizado y verificacion open; los
demas revisores aportan sus fuentes. Recalibra al riesgo residual comprobado. No combines una
verificacion closed con una decision open: coordina esa correccion antes de finalizar.

El ledger conserva todos los hallazgos abiertos/cerrados/aceptados/descartados, sus decisiones y
verificaciones por pasada, fixes, evidencia, decision externa, identidades y aliases. Solo la
curacion validada alimenta la memoria. Una verificacion puede cerrar un hallazgo; que un archivo
se haya revisado no lo cierra. Las etiquetas son NUEVO, REINCIDENTE neutral, REGRESION tras cierre
verificado, e INTRODUCIDO_POR solo con la evidencia explicita anterior.

`coverage` guarda tree/head/base/merge_base/pass_n. Un ref local
`refs/pre-pr-review/<hash-rama>/<tree>` mantiene vivo cada snapshot aceptado sin crear commits.
Los refs son locales; un clon nuevo cae a full si no tiene el arbol. No borres refs a mano durante
una corrida. La escritura del ref precede al reemplazo atomico del ledger: si falla el disco, puede
quedar un ref extra, pero la cobertura vieja sigue utilizable.

Dos finalizaciones del mismo ledger en esta maquina no se pisan: `ledger.py` toma un lock exclusivo
en `<git-common-dir>/pre-pr-review/<hash del ledger>.lock` (fuera de pr-reviews/, compartido entre
worktrees). La segunda falla con REVISION INCOMPLETA sin tocar el ledger; despues, su digest ya no
coincide y debe prepararse de nuevo. Un lock de mas de 15 minutos se considera abandonado. Entre
maquinas no hay lock: lo detecta Git como conflicto en `ledger.json` y se resuelve conservando una
de las dos pasadas y repitiendo la otra.

### Retencion

Despues de preparar, `retention.py` elimina corridas de `.pre-pr-review/` con mas de 30 dias que no
esten entre las 5 mas recientes de su rama y que ya no puedan finalizarse (finalizadas, sin cambios,
o preparadas contra un ledger que ya avanzo). De los refs de snapshots solo toca los de **la rama
actual**, y conserva la cobertura vigente, las ultimas 5 pasadas y los arboles de toda corrida
retenida. Nunca borra pr-reviews/, refs de otras ramas ni la corrida en curso, y un fallo de limpieza
no detiene la preparacion. `PRE_PR_RETENTION_DAYS=0` o `--no-cleanup` la desactivan.
`python3 scripts/retention.py --repo . [--days N] [--keep N]` muestra el plan; `--apply` lo ejecuta.

`accepted.json` requiere `aceptados: []`, con huella o categoria/archivo/simbolo y
`techo_severidad`. Solo el usuario decide aceptaciones. El techo se vuelve a aplicar a los registros
arrastrados y aliases; una severidad mayor vuelve a abrirlo. Los externos no cuentan para el
veredicto accionable, incluidos los arrastrados. Usa siempre conteos/veredicto de la clasificacion
final; en error reporta revision incompleta, sin inventar conteos vacios.
