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
  scripts/review_sync.py               sincronizacion opcional con una API Agent Autolearn (perfiles)
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

**Desde 2.16.0 — menos turnos y menos contexto, los mismos revisores.** Cada turno de un agente relee
todo su contexto, asi que el coste de una corrida es, sobre todo, *turnos × contexto*:

- **Conversacion larga.** El orquestador hace ~20 turnos y en cada uno relee la conversacion entera. En
  corridas reales lanzadas tras horas de trabajo en la misma sesion eso fue hasta un 70 % extra de tokens.
  `prepare_review.py` mide el contexto de la sesion (solo contadores de su transcript) y, por encima de
  ~150k tokens, el orquestador pregunta una vez si seguir o continuar en una sesion nueva: la corrida ya
  preparada se reanuda sola y el resultado es identico. `usage-summary.md` lo señala tambien a posteriori.
- **Cierre en un turno.** `review_usage.py finish` resume tokens, sincroniza y devuelve veredicto, conteos
  y bloqueantes; el orquestador ya no lee el informe ni `clasificacion.json` para entregar.
- **Patch grande en un turno.** `patch-index.json` da el rango de cada archivo dentro de `new.patch` para
  leerlo con lecturas paralelas en lugar de una por turno. Los prompts piden agrupar lecturas y busquedas
  independientes: la misma evidencia en menos turnos.
- **Arquitectura Next.js solo con superficie.** En incremental se omite unicamente cuando el delta JS/TS es
  solo cuerpo de archivos existentes: sin archivos nuevos/movidos, sin imports ni APIs de frontera cambiados,
  fuera de `_internal`/actions/route/layout/stores/schemas y sin cadenas cliente/servidor (el grafo se resuelve
  siempre). Una validacion completa nunca lo omite. Los revisores de stack leen un recorte sin tests ni otros
  lenguajes; el resto conserva el patch completo.
- **Lo que no se toco, a proposito.** edge-case, regression y test siguen entrando en deltas pequeños: en las
  corridas medidas encontraron un HIGH y varios MEDIUM que code-reviewer no vio. El agregador sigue
  redactando: reescribe la mayor parte del porque y del fix al verificar en el arbol, no copia.
- **Perfil de turnos.** Cada agente registra turnos, herramientas por turno y contexto pico (solo contadores);
  viaja en el diagnostico a Agent Autolearn para comprobar que de verdad se agrupan las lecturas.

Antes de dar por buena una version que cambie prompts o routing, compara contra la anterior con los casos de
`evals/` (`eval_review.py compare` falla si se pierde un bug sembrado).

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

### Sincronizar con Agent Autolearn (opcional)

Cada corrida puede enviarse a una API [Agent Autolearn](https://github.com/JoseLuis21/agent-autolearn) para ver
estadísticas, dar feedback y pedir mejoras de las skills desde su panel. Es opcional: sin perfil activo no se envía
nada, y un fallo de red o de la API nunca cambia el veredicto ni bloquea la revisión. Al terminar, el orquestador
encola la corrida (sin red) y la envía en segundo plano.

**Conceptos**

| | Qué es |
|---|---|
| **Instancia** | Una API Agent Autolearn desplegada. La actual es `https://agent-autolearn.josephluihs.workers.dev`. |
| **Workspace** | Un espacio aislado dentro de la instancia (por ejemplo «Personal» y el de tu empresa), con sus propios datos, miembros y configuración de IA. |
| **Token de instalación** | Credencial que emite un workspace. **El token decide a qué workspace llegan las revisiones.** |
| **Perfil** | En tu máquina: nombre + URL de la instancia + token. Un perfil por workspace. |
| **`.agent-autolearn.json`** | En cada repo: qué perfil usa ese repo. No contiene secretos. |

#### 1. Obtén un token por workspace

En el panel de la instancia:

1. Elige el workspace en el selector de la barra lateral.
2. Ve a **Workspace → Tokens → Crear token**.
3. Tipo **Instalación del plugin**; permisos **ingest** y **read**. Opcionalmente, limítalo a ciertos repositorios.
4. Copia el token (`alt_…`): se muestra una sola vez.

Repite para cada workspace al que quieras enviar revisiones. Necesitas ser administrador del workspace; si no lo
eres, pide el token a quien lo administra. Un token de un workspace no sirve para otro.

#### 2. Configuración rápida: un comando

Desde la raíz del repo que quieres conectar, en tu terminal (no dentro de Claude Code, porque pide el token):

```bash
python3 ~/.claude/plugins/marketplaces/agent-autolearn/plugins/agent-autolearn/scripts/review_sync.py setup personal
```

Pega el token cuando lo pida (no se muestra). `setup`:

1. Comprueba el token contra la API y te dice a qué workspace pertenece. Si la API lo rechaza, no guarda nada.
2. Guarda el perfil `personal` con la URL de la instancia (`https://agent-autolearn.josephluihs.workers.dev`, o la de
   `--url`/`AGENT_AUTOLEARN_URL`).
3. Asigna ese perfil al repo actual (`.agent-autolearn.json`). Fuera de un repo, se lo salta.
4. Añade el alias `review-sync` a tu `~/.zshrc` o `~/.bashrc` (una sola vez).

Para otro workspace, repítelo con otro nombre desde uno de sus repos: `review-sync setup empresa`. Opciones:
`--default` (usar este perfil en repos sin `.agent-autolearn.json`), `--no-repo`, `--no-alias`.

**`setup` solo asigna el perfil al repo donde lo ejecutas.** Cada repo que quieras sincronizar necesita su perfil
(salvo que uses `--default`); si no, sus revisiones no se envían y `review-sync status` dice «no hay perfil activo»:

```bash
cd ~/code/otro-repo && review-sync use personal     # una vez por repo
```

Si `review-sync` no existe todavía, abre una terminal nueva (el alias se carga al iniciarla). Comprueba con el paso 4.
Los pasos 2b y 3 explican lo mismo por partes.

#### 2 (Windows). Configuración rápida en PowerShell

Claude Code en Windows ejecuta los comandos del plugin con Git Bash, y el plugin busca Python 3.9+ como `python3`,
`python` o `py -3` (`scripts/py.sh`), así que el instalador de [python.org](https://www.python.org/downloads/) basta.
Requisitos: **Git for Windows** (incluye Git Bash) y **Python 3.9+**. El alias `python3` de la Microsoft Store no
cuenta: si al escribir `python` se abre la Store, desactívalo en *Configuración → Aplicaciones → Alias de ejecución*.

Desde la raíz del repo, en PowerShell:

```powershell
py -3 "$HOME\.claude\plugins\marketplaces\agent-autolearn\plugins\agent-autolearn\scripts\review_sync.py" setup personal
```

Hace lo mismo que en macOS/Linux, pero el atajo `review-sync` se crea como función en tu perfil de PowerShell
(`$PROFILE.CurrentUserAllHosts`). Abre una PowerShell nueva para usarlo. Si PowerShell dice que la ejecución de
scripts está deshabilitada, permite los scripts locales una vez:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Si lo ejecutas desde Git Bash, el alias va a `~/.bashrc` como en Linux. El resto de la guía es igual; en PowerShell,
para una sola corrida con otro perfil usa `$env:AGENT_AUTOLEARN_PROFILE = "otro"; claude`.

#### 2b. Configuración manual

```bash
S="$HOME/.claude/plugins/marketplaces/agent-autolearn/plugins/agent-autolearn/scripts/review_sync.py"
alias review-sync="python3 $S"      # opcional: añádelo a tu ~/.zshrc o ~/.bashrc

API=https://agent-autolearn.josephluihs.workers.dev

review-sync configure --profile personal --url $API --default   # pide el token sin mostrarlo
review-sync configure --profile empresa  --url $API
review-sync profiles                                             # nombre, URL y prefijo del token
```

- El token se pide sin eco, o se lee de `AGENT_AUTOLEARN_TOKEN`. **Nunca** va en la línea de comandos.
- Los perfiles se guardan en `~/.config/agent-autolearn/config.json` (permisos 600).
- `--url` es la raíz de la instancia (si le pones `/v1` al final, se quita solo). Todos los perfiles pueden apuntar a la
  misma URL: lo que cambia es el token.
- Volver a ejecutar `configure` con el mismo nombre reemplaza la URL y el token (sirve para rotar un token revocado).
- `--default` marca el perfil que usan los repos sin `.agent-autolearn.json`. Si no quieres que nada se envíe
  por defecto, no marques ninguno.

#### 3. Elige el workspace de cada repo

```bash
cd ~/code/repo-de-la-empresa && review-sync use empresa
cd ~/code/mi-proyecto        && review-sync use personal
```

`use` escribe `.agent-autolearn.json` (`{"profile": "empresa"}`) en la raíz del repo. Puedes commitearlo para que todo
el equipo envíe ese repo al mismo workspace: cada persona crea en su máquina un perfil con ese nombre y su propio token.
Si prefieres no commitearlo, añádelo a `.git/info/exclude`.

El perfil activo se decide en este orden:

1. `AGENT_AUTOLEARN_PROFILE` (útil para una corrida puntual: `AGENT_AUTOLEARN_PROFILE=personal claude`).
2. `.agent-autolearn.json` del repo.
3. El perfil por defecto.
4. Ninguno: la sincronización queda desactivada, sin error.

#### 4. Comprueba que apunta donde crees

```bash
cd ~/code/repo-de-la-empresa && review-sync status
```

```text
Perfil: empresa (.agent-autolearn.json del repo)
API: https://agent-autolearn.josephluihs.workers.dev  token alt_Ab12…
Workspace: <nombre del workspace> · como instalación:laptop-ana · permisos: ingest, read
Cola: 0 pendientes · 3 enviadas · 0 con error
```

La línea **Workspace** la responde la API con ese token: es el workspace donde aparecerán las revisiones. En el panel,
cambia a ese workspace para verlas.

#### 5. Uso diario

No hace falta nada: `/pre-pr-review` encola y envía al terminar. Comandos para cuando lo necesites:

```bash
review-sync push                  # envía lo pendiente (p. ej. tras estar sin conexión)
review-sync push --retry-failed   # reintenta lo que quedó con error, después de corregir la causa
review-sync status --json         # detalle por corrida: estado, id en la API y motivo del error
```

#### Problemas frecuentes

| Síntoma en `status` | Causa y solución |
|---|---|
| `Sincronizacion desactivada: no hay perfil activo` | El repo no tiene `.agent-autolearn.json` y no hay perfil por defecto. Ejecuta `review-sync use <perfil>`. |
| `— NO configurado en este equipo` | El repo pide un perfil que no existe en tu máquina. Créalo con `configure` y el token de ese workspace. |
| `Workspace: desconocido (… 401 …)` | Token revocado o mal copiado. Crea otro en el panel y vuelve a ejecutar `configure`. |
| Corridas `sync_failed` con `404` | El token está limitado a otros repositorios de ese workspace. Amplíalo en el panel o crea otro token. |
| `no se encontro Python 3.9+` (Windows) | Instala Python desde python.org y abre una terminal nueva. Si `python` abre la Microsoft Store, desactiva ese alias de ejecución. |
| Corridas `pending` | La API no respondió (red, 5xx). Se reintentan en el próximo `push` o en la próxima revisión. |
| Nota «no registrados» o «no hay commit» | Las revisiones llegan, pero sin la versión exacta de las skills, y el análisis con IA necesita esa versión. Ver abajo. |

**Versiones de skills y agentes.** Para que el panel pueda analizar y mejorar una skill, cada corrida registra el commit
exacto de los archivos que se ejecutaron. Claude Code ejecuta el plugin desde una copia sin git
(`~/.claude/plugins/cache/…`), así que el cliente busca el commit en el clon del marketplace
(`~/.claude/plugins/marketplaces/agent-autolearn`) o en el clon que indiques con `AGENT_AUTOLEARN_PLUGIN_REPO`, y solo
registra una versión si su contenido coincide byte a byte con lo que se ejecutó. Si editaste archivos del plugin a mano,
esos componentes se envían sin versión en vez de declarar una falsa. Para evitarlo, actualiza el plugin con
`/plugin update agent-autolearn@agent-autolearn`.

#### Qué se envía y qué no

- Se envían los hallazgos curados, la cobertura, el veredicto, el consumo de tokens por agente y las versiones usadas.
  El repositorio se identifica por su remoto `origin` sin credenciales (nunca por la ruta local).
- Desde 2.14.0 también va un diagnóstico de coste: el motivo del modo completo/incremental, el tamaño del patch
  por archivo (ruta, bytes y líneas +/−, nunca contenido), el motivo de cada revisor y cuántas búsquedas registró.
  Las corridas ya enviadas lo completan en el siguiente `push` si su carpeta sigue en disco. El consumo estimado del
  orquestador se vuelve a enviar solo al terminar el turno, para que la API no quede con la cifra previa.
- Antes de encolar se redactan claves y tokens, se omite la evidencia de archivos `.env`/`.pem`/`.key` y se recorta
  cada texto a 4000 caracteres. Nunca se envían patches ni transcripts.
- La cola y el progreso viven en `.pre-pr-review/sync/`, que nunca se commitea. Reenviar no duplica corridas ni tokens:
  cada paso lleva una `Idempotency-Key` estable. Los fallos temporales quedan `pending`; los de esquema o permisos quedan
  `sync_failed` con su motivo y no se reintentan solos.

## Publicar un cambio

1. Sube `version` en los **dos** JSON: `plugins/agent-autolearn/.claude-plugin/plugin.json` y
   `.claude-plugin/marketplace.json`. Si no, el equipo no ve el cambio.
2. Commit y push a `main`.
3. Cada dev: `/plugin update agent-autolearn@agent-autolearn` (requiere reiniciar Claude Code para aplicar).

Si el cambio toca solo el contenido de una skill o un agente, con eso basta. Si añade o quita
componentes, avisa al equipo: los nombres de agentes y skills cambian lo que ven en su sesión.
