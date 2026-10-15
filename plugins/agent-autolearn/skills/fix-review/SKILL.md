---
name: fix-review
description: Pregunta que rama corregir (por defecto la actual), trae su ultima revision pre-PR desde Agent Autolearn (o el ledger local) y corrige los hallazgos abiertos que el usuario elija, por severidad y con test primero. No marca hallazgos ni toca pr-reviews/; la siguiente pasada de /pre-pr-review los cierra. Usar al pedir solucionar, corregir o aplicar una revision, o invocar /fix-review.
---

# Fix Review

Corriges los hallazgos abiertos de la ultima revision pre-PR de una rama. La revision la hizo otro
(un `/pre-pr-review` local o el worker de PRs): tu trabajo es arreglar el codigo, no volver a revisar
ni decidir que un hallazgo esta cerrado. Solo la siguiente pasada de `/pre-pr-review` lo verifica y
lo cierra.

**Comandos con valores literales.** `$ROOT`, `$RUN_DIR` y las demas variables de los ejemplos son
marcadores: escribe su valor real (rutas absolutas) en cada comando. No ejecutes comandos con `$VAR`,
`${VAR}`, `$(...)` ni backticks: sin aprobacion interactiva (por ejemplo, el worker de PRs) Claude Code
los deniega aunque el comando este permitido, y la corrida queda incompleta.

## 1. Elegir la rama

`$ROOT` es la raiz del repo (`git rev-parse --show-toplevel`). Si el usuario indico la rama en el
mensaje o en `$ARGUMENTS`, usala. Si no, **dale a elegir de una lista**:

```bash
git -C "$ROOT" fetch --prune origin 2>/dev/null
git -C "$ROOT" symbolic-ref --quiet --short HEAD
git -C "$ROOT" for-each-ref --sort=-committerdate \
  --format='%(refname:short)|%(committerdate:relative)|%(subject)' refs/heads refs/remotes/origin
```

Quita `origin/HEAD`, `origin` a secas, development, main, master, prod y production, y deja una
sola entrada por rama (sin el prefijo `origin/`; si existe local y remota, gana la local). Pregunta
con **AskUserQuestion**: «¿Que rama quieres corregir?». Si la rama actual es candidata, va primera
con «(Recomendado)» en la etiqueta; el resto, las mas recientes hasta 4 opciones (etiqueta = nombre
exacto de la rama; descripcion = fecha relativa y asunto del ultimo commit). Si hay mas, indica que
puede escribirla en «Other». Sin AskUserQuestion, muestra la lista numerada.

Los arreglos van en el checkout de esa rama. Si no es la actual, cambia solo con el arbol limpio
(`git status --porcelain` vacio); con cambios locales, pide al usuario que deje la rama disponible:
sin stash, descarte ni traslado automatico. Si la rama no existe, pide corregir el nombre; no la
crees.

Trae sus ultimos cambios, solo en avance rapido:

```bash
git -C "$ROOT" ls-remote --exit-code --heads origin "<rama>" >/dev/null 2>&1 \
  && git -C "$ROOT" pull --ff-only origin "<rama>"
```

Si el pull se niega (rama divergente o cambios que chocan), detente y explica; no hagas merge,
rebase ni reset. Sin remoto o sin red, sigue con lo local y dilo en una linea.

## 2. Traer la ultima revision

```bash
sh "${CLAUDE_PLUGIN_ROOT}/scripts/py.sh" review_sync.py latest --repo "$ROOT" --branch "<rama>"
```

Imprime un JSON con `source`, `run`, `latest_attempt`, `open_findings`, `settled_by_humans`,
`counts` y `notes`. Primero consulta Agent Autolearn, que tiene tambien las revisiones del worker
de PRs y quita lo que una persona descarto en el panel; si no hay perfil o la API no responde, usa
el ledger local de la rama (`source: "ledger"`). Si el script no esta disponible pero tienes la
herramienta MCP `get_branch_review` de agent-autolearn, llamala con `remote` = la salida de
`git remote get-url origin` y `branch` = la rama: devuelve la misma forma.

- `source: null` o `run: null`: no hay revision de esa rama. Dilo, muestra las `notes` y sugiere
  `/pre-pr-review <rama>`. No sigas.
- `open_findings` vacio: no queda nada abierto. Dilo (y cuantos estan en `settled_by_humans`) y
  termina.
- `latest_attempt` con valor: hay una revision mas nueva sin resultado (`running` o `failed`).
  Avisa en una linea que trabajas con la anterior.
- Menciona en una linea el origen (workspace o ledger) y las `notes` que expliquen un fallback.

**Comprueba si el codigo cambio desde la revision.** `run.head_commit` es el commit revisado:

```bash
git -C "$ROOT" merge-base --is-ancestor "<head_commit>" HEAD && \
  git -C "$ROOT" log --oneline "<head_commit>..HEAD"
```

Si hay commits nuevos, dilo: algunos hallazgos pueden estar ya resueltos y las lineas pueden haberse
movido. Si `head_commit` no es ancestro de HEAD (rebase o force push), avisa de que las referencias
son aproximadas. En ambos casos, cada hallazgo se comprueba contra el codigo actual antes de tocarlo.

## 3. Elegir que corregir

Muestra los hallazgos en una tabla, en el orden recibido (ya vienen por severidad):

| # | Severidad | Archivo:linea | Titulo |

Separa dos grupos que **no se corrigen con codigo** y listalos aparte, con su motivo:
- `external_decision` con valor (`infra`, `producto`, ...): necesitan una decision o una accion fuera
  del codigo. Resume lo que pide su `fix` como tarea para una persona.
- `human_judgment.judgment == "needs_investigation"`: alguien pidio investigarlo; corrigelo solo si el
  usuario lo elige expresamente.

Pregunta con **AskUserQuestion**: «¿Que hallazgos corrijo?», con opciones:
«Los bloqueantes (BLOCKER y HIGH)» (con «(Recomendado)» si hay alguno), «Todos los abiertos»,
«Elegir uno a uno» y «Ninguno». Con «Elegir uno a uno», pregunta por numeros de la tabla. Espera la
respuesta antes de editar.

## 4. Corregir

Uno a uno, de mayor a menor severidad:

1. **Comprueba que sigue vigente.** Lee el codigo en `file` (cerca de `line` y de `symbol`) y busca
   la `evidence`. Si ya no aplica, no lo toques: anotalo como «ya resuelto» con el motivo.
2. **Entiende el porque.** `why` explica el riesgo; `fix` dice que cambiar y su criterio de cierre
   (los casos que deben pasar). El criterio de cierre es lo que la siguiente revision comprobara.
3. **Test primero en bugs.** Si el hallazgo es un bug o un hueco de tests, escribe primero el test que
   falla con el codigo actual, verifica que falla y luego corrige (skill `testing`). Si no es testeable
   (configuracion, documentacion), dilo.
4. **Cambio minimo y dentro del alcance.** Solo lo que pide el hallazgo. Respeta las skills del stack
   del repo: `development-golang` (Go hexagonal), `development-nextjs` (Next.js), `owasp-security`,
   `resilience` y `safe-refactor` cuando el arreglo toque su terreno.
5. **Si el arreglo exige una decision** (producto, contrato publico, migracion de datos, borrar algo
   compartido) o el `fix` es ambiguo, pregunta en vez de adivinar.

Si el usuario cree que un hallazgo no es real, no lo corrijas ni lo marques tu: indica que puede
juzgarlo en el panel de Agent Autolearn («No es un problema»), y entonces dejara de aparecer.

Al terminar, ejecuta los tests y el lint del proyecto (`make test`, `go test ./...`, `pnpm test`,
lo que use el repo) y arregla lo que hayas roto.

## 5. Entregar

- Resumen por hallazgo: corregido (con el test que lo cubre), ya resuelto, omitido o pendiente de
  decision, con `archivo:linea`.
- **No toques** `pr-reviews/` (ledger, informes, `accepted.json`) ni cambies el estado de ningun
  hallazgo: no hay forma de cerrarlos a mano.
- **No commitees ni pushees sin permiso.** Ofrecelo con AskUserQuestion: «¿Commiteo los arreglos?»,
  con «Commitear y pushear», «Solo commitear» y «No». Commitea solo los archivos que tocaste, nunca
  `pr-reviews/`, con un mensaje que nombre los hallazgos corregidos. Si el push se rechaza, no hagas
  pull, rebase ni force.
- Cierra con el siguiente paso: `/pre-pr-review <rama>` para verificar y cerrar los hallazgos, o, si
  la rama tiene un PR abierto con el worker activo, el push dispara una revision nueva.
