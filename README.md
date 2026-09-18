# Agent Autolearn — plugin para Claude Code

Marketplace de plugins internos para Claude Code.

## Plugin `agent-autolearn`

Un solo plugin con dos cosas que se apoyan la una en la otra:

| | Qué es | Cuándo actúa |
|---|---|---|
| **Skills de arquitectura** | La ley de cada stack, en `skills/development-*` | Mientras escribes código. Claude las carga solo al detectar el stack |
| **`/pre-pr-review`** | Revisión multi-agente del diff | Antes de abrir el PR. Verifica **esas mismas leyes** |

La ley se escribe una vez y sirve para las dos cosas: quien programa la lee para hacerlo bien, y el
revisor la lee para comprobarlo. No hay dos versiones que se puedan separar.

### Skills

| Skill | Para | Cubre |
|---|---|---|
| `development-nextjs` | Proyectos Next.js (App Router, pnpm, AuthJS, shadcn) | DAL en `_internal`, frontera cliente/servidor, Server Actions, Zod en las dos fronteras, DTOs, estructura de módulos, Zustand, caché |
| `development-golang` | Backends Go hexagonales (`internal/core/` con `ports/ adapters/ domain/ services/`) | Regla de dependencias, flujo DTO → Command/Query → Filters/Update → dominio → Result, value objects, container, estándares Go |
| `owasp-security` | Los dos stacks | Código seguro contra OWASP: placeholders y `ORDER BY` por `switch`, IDOR y scope por dueño en el filtro, los helpers de auth y cripto que ya existen (`utils.HashPassword`, `jwt.WithValidMethods`), SSRF y redirects |
| `testing` | Los dos stacks | Tests que cazan el bug: la regla del test que falla antes del fix, la testabilidad como restricción de diseño, helpers que tapan lo que debían detectar, cubrir la rama que devuelve y no solo la que rechaza, y el gate que corre cero tests en verde |
| `resilience` | Los dos stacks | Degradar sin romper: distinguir «vacío» de «falló», campos del CMS que faltan, timeout en toda llamada saliente, fechas en UTC, variables de entorno validadas (`parseInt("1h")` es `1`), fallar cerrado |
| `safe-refactor` | Los dos stacks | Unificar sin regresiones: diffear las copias antes de fundirlas, los defaults y los sanitizadores son comportamiento, encontrar los callers que están fuera del diff, identificadores compartidos que deben seguir compartiéndose |

No hay que instalarlas por separado ni copiarlas a cada repo: vienen con el plugin y se activan
solas según el proyecto en el que estés. Si un repo documenta su propia arquitectura
(`rules.txt`, `AGENTS.md`, `CLAUDE.md`, su copia de la skill), **esa manda** sobre la del plugin.

### `/pre-pr-review`

Revisión multi-agente del diff **antes** de abrir un PR.

```text
/pre-pr-review [rama-del-pr] [--full | --verify-pending]  # siempre contra development
  → prepare_review.py: snapshot real, delta, contexto y asignaciones
  → revisores asignados en paralelo: seguridad + correctitud/especialistas segun riesgo
  → review-aggregator: valida evidencia, cura findings y descartes
  → ledger.py: valida cobertura y persiste las decisiones finales
  → pr-reviews/pr-<rama>-<hash>/<fecha>-p<N>.md
```

Seguridad corre para todo delta no excluido, incluidos docs/tests/config y lockfiles. Code cubre
correctitud y, en cambios pequeños, limites/contratos/tests. Los especialistas se activan por
riesgo, validacion completa o pendientes; convenciones solo con candidatos del escaner o pendientes.
Todas las omisiones llevan motivo en `run.json`. Fallos de revisores asignados son cobertura incompleta.

**Veredictos:** `LISTO PARA PR` · `CORREGIR ANTES DE SUBIR` (hay HIGH) · `NO SUBIR` (hay BLOCKER).

Stacks cubiertos por la guía general: **Next.js, Golang, PHP CodeIgniter, AWS Lambda**
(`skills/pre-pr-review/references/stacks.md`).

#### Revisores de arquitectura

Solo se lanza el que corresponde al repo, detectado por su estructura, y solo si el diff trae
código de ese stack:

| Revisor | Corre si | Verifica |
|---|---|---|
| `nextjs-architecture-reviewer` | hay `next.config.*` o la dependencia `next` **y** el delta actual toca JS/TS, o hay pendientes propios | `development-nextjs` |
| `go-architecture-reviewer` | hay `go.mod` y estructura `internal/core/` con `ports`, `adapters`, `domain` o `services`, y el delta toca `.go`; o hay pendientes propios | `development-golang` |

La segunda condición es de coste: un PR que solo toca `.md` no necesita que un revisor arranque,
lea una ley de 26KB y cierre con `findings: []`.

En un repo que no es ninguno de los dos no se lanza ninguno, y eso **no** cuenta como cobertura
incompleta. La puerta del de Go es la **estructura**, no el tipo de proyecto: casi todos los
microservicios `*-ms` (workers y lambdas) son hexagonales igual que la API — cambia el lado que entra
(`driving/lambda_handler/` en vez de `controllers/`), no el core.

En Next.js, la preparación resuelve además el **grafo de imports** del snapshot con
`scripts/scan_client_server.py` y deja en `_client-server-raw.json` las cadenas donde un archivo
`'use client'` alcanza —directa o transitivamente— un módulo con `server-only`, `next/headers` o
un builtin de Node. Eso no es una opinión de arquitectura: **rompe el build de producción**, y un
modelo leyendo un diff no sigue esa cadena de forma fiable. El revisor confirma cada cadena
abriendo sus archivos y la reporta como BLOCKER. Un `'use server'` en el módulo importado corta la
cadena y no es hallazgo: ahí la frontera es legal.

Cada revisor lee la skill (la ley) y su calibración (`references/<stack>-architecture.md`, que dice
la severidad de cada violación y **qué no es hallazgo**). El diff decide qué se reporta: el código
viejo que nadie tocó no frena el PR, y una migración entera sale como un único MEDIUM con todas las
ubicaciones, marcado como decisión externa.

#### Cobertura OWASP

`security-reviewer` audita contra **OWASP Top 10 (2021)** en el lado web y **API Security Top 10
(2023)** en el lado API. Un PR full-stack pasa por las dos listas.

No se persigue cumplimiento del 100% — una revisión mira un diff, no la aplicación entera, y
prometer lo contrario es teatro. Lo que sí se exige son dos niveles:

| Nivel | Categorías | Qué implica |
|---|---|---|
| **Paso obligatorio** | A03/API8 inyección · A01/API1/API3/API5 control de acceso · A07/API2 autenticación · A02 criptografía · A10/API7 SSRF | Se recorren **en cada revisión**, toque o no el diff su superficie, y el resultado se declara en el informe |
| **Solo si el diff lo toca** | A04 diseño · A05 configuración · A06 dependencias · A08 integridad · A09 logging · API4 recursos · API6 flujos de negocio · API10 consumo de APIs | Se miran cuando el diff toca su superficie (config, lockfiles, logs, webhooks) |

Cada hallazgo lleva su categoría en el campo `owasp`, y las cinco obligatorias aparecen en la tabla
de cobertura del informe con **lo que se miró en cada una** — `A03 ok (3 queries parametrizadas)`,
no `seguridad: ok`. Esa línea es la diferencia entre "mirado y limpio" y "no mirado".

Sigue vigente la regla que evita el teatro inverso: **ninguna categoría se reporta sin una línea
concreta del diff detrás.** "El PR debería considerar A04" no es un hallazgo. Criterio completo en
`skills/pre-pr-review/references/owasp.md`, y el **cómo escribirlo bien** en la skill
`owasp-security`, que es la que lee `security-reviewer` para proponer el fix.

#### Convenciones obligatorias del equipo

La preparacion ejecuta una vez `scripts/scan_conventions.py`. `convention-reviewer` solo corre
si hay candidatos o pendientes y confirma cada hit leyendo el archivo real. Las tres son **HIGH**: frenan el PR hasta limpiarlas.

| Regla | Qué marca | Excepciones que no marca |
|---|---|---|
| **Todo en inglés** | Identificadores, comentarios y **strings** en español, incluido camelCase (`usuarioActual`) y conjugaciones (`"Lead guardado"`) | Columnas de BD legadas y campos de APIs externas en español, archivos de traducción `es`, datos de prueba |
| **Sin debug olvidado** | `console.log/debug/table`, `debugger`, `fmt.Println`, `spew.Dump`, `var_dump`, `print_r`, `dd()`, `error_log` | `console.error/warn`, loggers reales, `fmt.Print*` en CLI y `cmd/`, tests y `scripts/` |
| **Sin URLs de desarrollo** | `localhost`, `127.0.0.1`, `.local`, ngrok, `http://host:3000` hardcodeados | Defaults de config (`process.env.X ?? "http://localhost:3000"`), `.env.example`, docker-compose, tests, bind a `0.0.0.0` |

Para ajustar las listas de palabras o de patrones, edita `scripts/scan_conventions.py`;
para ajustar las excepciones, `agents/convention-reviewer.md`.

## Instalación (cada dev, una vez)

```
/plugin marketplace add https://github.com/JoseLuis21/plugin-agent-autolearn.git
/plugin install agent-autolearn@agent-autolearn
```

Con eso quedan disponibles las skills **y** `/pre-pr-review`. No hay nada que copiar a los repos.

Rama del marketplace: `main`.

Con el repo clonado en local, para probar cambios antes de subirlos:

```
/plugin marketplace add /Users/tu-usuario/ruta/plugin-agent-autolearn
/plugin install agent-autolearn@agent-autolearn
```

## Uso

**Las skills no se invocan**: Claude carga la del stack cuando detecta que estás escribiendo código
de ese stack. Si quieres consultarla a mano, `/agent-autolearn:development-nextjs` (o `-golang`).

**La revisión, antes de abrir el PR:**

```
/pre-pr-review              # pregunta que rama existente revisar contra development
/pre-pr-review feature/leads # revisa esa rama; incremental tras la primera cobertura
/pre-pr-review --full       # repetir revision completa solo cuando se solicita
/pre-pr-review --verify-pending # revalidar pendientes aunque el contenido no cambie
```

Si no indicas la rama, pregunta cual debe revisar; si ya la indicaste, la reutiliza. No crea una
rama nueva ni toma el nombre recibido como base: la comparacion sigue siendo contra development.
Usa el checkout de esa rama y conserva su ledger. Si hay cambios locales en otra rama, pide dejar
disponible la rama indicada sin mover ni descartar esos cambios.

Revisa el contenido neto de la rama **y** staged, unstaged y untracked no ignorados. Cada corrida deja un archivo nuevo en
`pr-reviews/pr-<rama>-<hash>/<fecha>-p<N>.md`, versionado con el código: las pasadas sucesivas sobre la misma
rama quedan una al lado de la otra.

El flujo **solo lee y reporta**: no modifica código, no commitea, no abre el PR.
Si hay BLOCKER o HIGH, Claude ofrece corregirlos — pero no toca nada sin que se lo pidas.

## Estructura

```
.claude-plugin/marketplace.json        catálogo del marketplace
plugins/agent-autolearn/
  .claude-plugin/plugin.json           manifiesto del plugin
  skills/
    development-nextjs/SKILL.md    LEY de arquitectura Next.js
    development-golang/SKILL.md    LEY de arquitectura Go hexagonal
    owasp-security/SKILL.md            LEY de código seguro (los dos stacks)
    testing/SKILL.md               LEY de tests (los dos stacks)
    resilience/SKILL.md            LEY de resiliencia y edge cases (los dos stacks)
    safe-refactor/SKILL.md         LEY de refactor sin regresiones (los dos stacks)
    pre-pr-review/
      SKILL.md                         orquestación: alcance → lanzar → agregar
      references/findings-contract.md  esquema JSON + las 11 reglas duras (lo lee todo revisor)
      references/findings-rules.md     apéndice: el porqué de las reglas, a consulta
      references/review-state.md       CLI, schemas v2 y recuperacion de corridas
      references/stacks.md             guía general por Next.js / Go / CodeIgniter / Lambda
      references/owasp.md                cobertura OWASP: obligatorio vs oportunista
      references/nextjs-architecture.md  calibración: severidad y qué NO es hallazgo
      references/go-architecture.md      calibración: severidad y qué NO es hallazgo
  scripts/scan_conventions.py          escáner determinista de convenciones
  scripts/scan_client_server.py        grafo de imports: frontera cliente/servidor de Next.js
  scripts/prepare_review.py            snapshot, delta y routing deterministas
  scripts/ledger.py                    validacion y persistencia final entre pasadas
  scripts/shared_checks.py             validacion por script de los hechos de build/tests
  scripts/review_usage.py              tokens por corrida, orquestador estimado y comparacion
  scripts/retention.py                 limpieza de corridas y snapshots antiguos
  scripts/eval_review.py               evaluaciones con bugs sembrados (evals/)
  agents/                              los 8 revisores + el agregador
```

Las leyes viven **solo** en las skills. Las referencias `*-architecture.md` no las repiten: califican.
Si cambia una regla, se toca un archivo.

## Personalizar

| Quieres | Edita |
|---|---|
| **La arquitectura de un stack** (la ley) | `skills/development-<stack>/SKILL.md` |
| Severidad de una violación / qué no es hallazgo | `skills/pre-pr-review/references/<stack>-architecture.md` |
| Reglas generales de tu stack | `references/stacks.md` |
| Cuándo se lanza cada revisor de arquitectura | `scripts/prepare_review.py`, funcion `route` |
| Añadir un stack nuevo | Nueva skill `development-<stack>` + su calibración + su agente |
| **Cómo se escribe código seguro** (la ley) | `skills/owasp-security/SKILL.md` |
| Qué categorías OWASP son de paso obligatorio | `references/owasp.md` |
| **Cómo se escriben los tests** (la ley) | `skills/testing/SKILL.md` |
| **Cómo se aguantan los edge cases** (la ley) | `skills/resilience/SKILL.md` |
| **Cómo se unifica código sin regresiones** (la ley) | `skills/safe-refactor/SKILL.md` |
| Palabras/patrones de las convenciones | `scripts/scan_conventions.py` |
| Módulos que no pueden llegar al cliente | `scripts/scan_client_server.py`, `SERVER_MARKERS` y `NODE_BUILTINS` |
| Excepciones de las convenciones | `agents/convention-reviewer.md` |
| Qué busca un revisor | `agents/<nombre>.md` |
| Formato del informe | `agents/review-aggregator.md`, "Paso 4" |
| Umbral del veredicto | `agents/review-aggregator.md`, sección "Paso 3" |
| Bajar coste | Routing y reutilizacion de contexto/consultas (ver abajo) |
| Qué queda fuera de los patches | `scripts/prepare_review.py`, filtros del snapshot y patch |
| Cuánto código lee cada revisor | `pre-pr-review/SKILL.md`, prompt compartido del Paso 2 |
| El porqué de una regla de hallazgo | `references/findings-rules.md` |
| Qué se puede leer pero no juzgar | `scripts/prepare_review.py`, `EXCLUDED_DIRS` |
| Cómo se etiquetan los hallazgos entre pasadas | `scripts/ledger.py` |
| Qué se decide no arreglar | `pr-reviews/accepted.json` del repo revisado |
| Añadir un revisor | Nuevo `agents/<x>.md` + routing y contrato en `scripts/prepare_review.py` |

### Pasadas sucesivas sobre la misma rama

La cobertura se ancla al **arbol Git del contenido realmente revisado**, mediante un indice temporal
que no modifica el indice real, worktree ni stash y no crea commits. Un delta se calcula una vez
entre ese arbol y el actual: repetir con cambios dirty iguales o commitearlos no los revisa de nuevo;
una reversion neta elimina el cambio. Los snapshots aceptados se mantienen vivos mediante refs locales.

Para continuar desde otro equipo, comparte codigo y `pr-reviews/` en la misma rama por push/pull.
Si falta el snapshot, el helper lo reconstruye desde el commit revisado o hasta 50 descendientes
recientes disponibles y exige coincidencia exacta del hash. Esto usa Git local, sin revision de IA.
Los cambios sin commit solo son recuperables si despues se compartio un commit con ese contenido.
No modifica el indice ni el ledger al recuperar. `.pre-pr-review/` sigue ignorado; las metricas y
consultas locales no se transfieren. Cambios conocidos de filtros invalidan la cobertura.

La primera pasada, `--full`, un ledger antiguo, una base cambiada, un rebase o un snapshot no recuperable
fuerzan revision completa conservando historial y numeracion. Una corrida sin contenido nuevo no
lanza agentes salvo revalidacion explicita. Abrir el PR no fuerza otro `--full`.
Si ya hubo revision y la cobertura dejo de ser reutilizable, explica el motivo y pregunta antes
de lanzar otro full. Un `--full` solicitado expresamente ya autoriza esa revision completa.

La base siempre es `development`, independientemente de `origin/HEAD` o del destino del PR.
Se prefiere `origin/development`, con fallback solo a `development` local; si no existe, se detiene.
No se permite revisar contra main, master, prod o production. Los alias de development con el
mismo merge-base mantienen la cobertura incremental.

Cada pendiente activo se asigna una vez a su dueño cuando hay delta/revalidacion. Solo una
verificacion explicita por huella con status y evidencia permite cerrarlo o descartarlo; archivo
tocado o ausencia del finding no bastan. Los abiertos arrastrados siguen contando para el veredicto.
El agregador cura primero, y `ledger.py` valida despues las fuentes, decisiones y cobertura antes
de persistir. Resultado ausente/invalido o codigo cambiado durante la revision deja la corrida
incompleta y conserva la cobertura previa.

El ledger guarda abiertos, cerrados, aceptados y descartados con evidencia, fix, severidad final,
`decision_externa`, revisores y aliases al fusionar causas raiz. `REINCIDENTE` es neutral.
`INTRODUCIDO_POR` solo se admite con evidencia causal antes/despues, nunca por compartir simbolo.

`pr-reviews/accepted.json` expresa decisiones humanas: el agente solo lo modifica por instruccion
expresa del usuario, con su motivo. El techo de severidad se aplica tambien a hallazgos arrastrados;
un LOW aceptado vuelve a contar si sube a HIGH. Los externos siguen visibles fuera del conteo accionable.

Una corrida interrumpida se reanuda sola: si codigo, ledger, reglas y asignaciones no cambiaron,
el helper devuelve la misma corrida y solo se lanzan los revisores que faltan. Un descarte previo
viaja como contexto a los revisores de ese archivo y solo se reabre con `new_evidence`; nunca se
suprime por script. Si cambian las reglas de un revisor (su agente, su ley o el contrato), el
helper lo avisa y el usuario decide entre repetir solo ese revisor sobre la rama o conservar la
cobertura: nunca fuerza un full. Dos finalizaciones simultaneas del mismo ledger no se pisan (lock
local en el directorio Git), y las corridas y snapshots antiguos se limpian con una politica
conservadora (30 dias, ultimas 5 por rama, nunca la cobertura vigente ni otras ramas).

Los directorios locales `.pre-pr-review/<corrida>/` conservan artifacts para auditoria/resume; no se
versionan. Solo informes y ledger van a `pr-reviews/`, sin snapshots completos de fuentes.
Los nombres de rama llevan hash para evitar colisiones; carpetas antiguas se reutilizan cuando
el ledger identifica la misma rama. CLI, esquemas y limites: `references/review-state.md`.

### Coste de una corrida

Build/tests tienen un unico responsable por corrida (`run.check_owner`): termina su revision
primero y publica comandos, alcance, entorno y resultados en `shared-checks.json`. Los demas
revisores corren despues en paralelo, manteniendo su analisis independiente. Solo reutilizan
resultados aplicables al mismo snapshot; escenarios distintos o evidencia dudosa justifican otra
ejecucion. Esto puede aumentar la espera inicial, pero evita repetir los mismos comandos.
Las consultas se publican durante la revision en archivos separados y se consultan antes de buscar;
pueden coincidir busquedas simultaneas. No se reduce el numero de revisores, modelos ni checklists.

El agregador usa `ledger.py --summary`: recibe conteos y rutas, mientras evidencia e historiales
completos siguen en los artifacts. Ni el agregador ni el orquestador vuelcan todo el ledger para
resumirlo. Esta reduccion de texto no filtra hallazgos ni cambia los criterios del veredicto.

La revision termina al alcanzar LISTO PARA PR: MEDIUM/LOW/NIT quedan visibles como seguimiento
no bloqueante, sin sugerir otra ronda hasta cero hallazgos. `clasificacion.json` separa las huellas
`bloqueantes` y `seguimiento`, y calcula `resumen.review_complete`; cobertura incompleta nunca cierra
el ciclo. Una revision nueva solicitada sigue evaluando el delta y puede detectar bloqueantes reales.
El agregador concilia recomendaciones que interactuan antes de entregarlas. Cada fix de bug incluye
su criterio de cierre y test necesario; la proteccion pendiente se conserva bajo el hallazgo original
con severidad residual, en lugar de generar una tarea nueva por cada pasada.

El ahorro viene de no repetir trabajo: snapshot y delta calculados una vez, routing por la ventana
actual, escaner de convenciones unico, contexto compartido y consultas reutilizables. Cada revisor
lee el patch una vez y solo la funcion/contexto que necesita. Full.patch queda disponible a demanda.
Code declara los checklists de los especialistas omitidos; `--full` se reserva para solicitudes explicitas.

Completa ubicaciones en la ventana y sigue callers afectados cuando el cambio lo justifica. Copias
preexistentes fuera del alcance van a seguimiento con una muestra; no se exige un censo del repo
por cada hallazgo ni se amplian los fixes de este PR con deuda ajena.

Generados/builds/vendor e instrumental quedan fuera del snapshot. Los lockfiles siguen participando
en identidad y routing: seguridad consulta los cambios de resolucion a demanda aunque no se pegue
su texto al patch compartido. `AGENTS.md` y `CLAUDE.md` del repo siguen siendo documentos revisables.

Los modelos mantienen su configuracion: Opus para code/security/edge/regression/aggregator;
Sonnet para test/convention/arquitectura. Bajar de modelo no sustituye eliminar lecturas duplicadas.
El flujo mejora convergencia y coste sin prometer que una revision encontrara todos los defectos.

### Medicion de tokens por corrida

El hook local `SubagentStop` captura los contadores del transcript de cada revisor y del agregador.
No agrega llamadas al modelo ni cambia la cobertura. Al terminar, el orquestador genera
`usage-summary.json` y `usage-summary.md` en la carpeta de la corrida: desglose de entrada,
salida registrada, cache creada/leida, peticiones y ranking por revisor. La marca `PRE_PR_USAGE`
en los prompts vincula cada agente a su corrida; los reintentos se suman sin duplicar callbacks.

El hook `Stop` estima aparte el consumo del orquestador (ventana de la conversacion principal entre
preparar y resumir la corrida). El resumen incluye version del plugin, consultas repetidas entre
revisores y hallazgos; una copia solo con contadores viaja con la rama en `pr-reviews/<rama>/usage/`
y `review_usage.py compare A B` enfrenta dos corridas. `clasificacion.json.metricas` da falsos
positivos, duplicados fusionados y rondas hasta aprobacion. `shared_checks.py` valida por script
los hechos de build/tests antes de que otros revisores los reutilicen.

Para comprobar que un cambio del plugin no pierde calidad hay evaluaciones con bugs sembrados
(`plugins/agent-autolearn/evals/README.md`): preparar y puntuar no gasta tokens; la revision del
fixture si, y solo se lanza a mano.

El subtotal excluye el orquestador y ejecuciones no capturadas; datos faltantes son N/D, nunca cero.
No equivale a una factura: la salida registrada puede ser provisional segun runtime. No se calculan
precios ni se usa `total_tokens` de la ultima peticion como consumo acumulado. Detalles y comando
manual: `plugins/agent-autolearn/skills/pre-pr-review/references/token-usage.md`.

## Publicar un cambio

1. Sube `version` en los **dos** JSON: `plugins/agent-autolearn/.claude-plugin/plugin.json` y
   `.claude-plugin/marketplace.json`. Si no, el equipo no ve el cambio.
2. Commit y push a `development`.
3. Cada dev: `/plugin update agent-autolearn@agent-autolearn` (requiere reiniciar Claude Code para aplicar).

Si el cambio toca solo el contenido de una skill o un agente, con eso basta. Si añade o quita
componentes, avisa al equipo: los nombres de agentes y skills cambian lo que ven en su sesión.
