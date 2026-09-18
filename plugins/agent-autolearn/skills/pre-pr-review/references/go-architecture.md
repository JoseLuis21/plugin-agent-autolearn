# Calibracion de revision — arquitectura Go hexagonal

**La ley esta en `${CLAUDE_PLUGIN_ROOT}/skills/development-golang/SKILL.md`.** Este archivo no
la repite: dice **con que severidad** pesa cada violacion y, sobre todo, **que no es hallazgo** —
que es donde un revisor hace daño.

Lee la skill primero. Sin ella, esto son severidades sin reglas.

---

## Cuando aplica

**No todo repo Go del equipo es hexagonal**, pero la mayoria si: casi todos los microservicios
`*-ms` (workers y lambdas) comparten el mismo `internal/core/`. Lo que cambia entre ellos y la API es **el lado que
entra**, no el hexagono:

| | API de referencia | Worker / lambda (`*-ms` (workers y lambdas)) |
|---|---|---|
| Lado que entra | `internal/controllers/` + `mappers/` (Fiber) | `internal/driving/lambda_handler/` |
| Tipo de frontera | `XRequestDTO` / `XResponseDTO` | payload del evento |
| Core | identico | identico |

Las reglas del **core** (regla de dependencias, ports, adapters, dominio, value objects, container)
aplican a los dos. Las reglas de **HTTP** (controlador, `http_response`, rutas publicas/privadas,
`c.Context()`) solo donde hay capa HTTP. En un worker, el handler hace de controlador: traduce el
evento a Command/Query y **nunca deja que el tipo del evento entre al servicio**.

Los que quedan fuera son los repos Go planos, sin `internal/core/` (algun `ntsmhf-*` viejo).
Revisarlos con esta ley produce treinta hallazgos inutiles.

Aplica si se cumple **al menos una**:

1. Existe `internal/core/<modulo>/` con al menos dos de `ports/`, `adapters/`, `domain/`, `services/`.
2. El diff **crea** esa estructura (modulo nuevo en un repo que empieza).

Si no aplica, `findings: []` y fuera. Los bugs de Go corrientes — errores ignorados, `defer`
faltante, goroutines sin contexto — ya los cubren `code-reviewer` y `stacks.md`; no son tuyos.

## Fuente de verdad

La skill del plugin es la ley por defecto. Si el repo revisado documenta la suya — `rules.txt`,
`AGENTS.md`, `CLAUDE.md` o una skill propia — **esa manda**: leela antes de juzgar y respeta sus
adaptaciones y sus deudas reconocidas. Donde difieran, gana la del repo y lo anotas en
`sin_hallazgos_en`. La API de referencia tiene la suya en `rules.txt`.

---

## 1. La regla de dependencias

Es el corazon de la arquitectura y la fuente de casi todos los hallazgos que importan. La tabla de
que puede importar cada capa esta en la seccion 3.1 de la skill. Contrastala import a import: es
mecanico, o el import esta o no esta.

**BLOCKER**
- `fiber` importado en cualquier archivo bajo `core/`. Rompe la razon de ser del hexagono: el
  dominio pasa a no poder testearse ni reutilizarse fuera de HTTP.
- `database/sql`, un driver o un SDK de AWS importados en `services/` o `domain/`.

**HIGH**
- Un servicio que depende del **struct concreto** de otro servicio en vez de su `ports.XService`.
- `adapters` importado desde `controllers` o desde `services`.
- `dto` importado desde `services` o desde `adapters`.

**Como verificarlo**: `rg -n 'gofiber|database/sql|aws-sdk' src/internal/core/` y la lista de imports
del archivo tocado. Es mecanico: o el import esta o no esta.

**Fix**: nombra la interfaz que falta y donde declararla (`ports/service.go` del modulo consumidor),
no "invertir la dependencia".

---

## 2. El flujo de tipos

```txt
RequestDTO -> Command/Query -> Service -> Filters/Update -> Repository -> Domain
           -> Result/Domain -> ResponseDTO
```

Cada flecha es una conversion con nombre. Ninguna capa recibe un tipo que pertenece a una capa dos
pasos mas alla.

**HIGH**
- Un DTO que entra a un servicio, o que sale de el.
- Un `Command`/`Query`/`Result` que llega a un repositorio.
- Un repositorio que devuelve un DTO, una fila cruda o un `sql.NullString`.
- Una entidad de dominio serializada directamente a la respuesta HTTP, sin `ResponseDTO`.

**MEDIUM**
- Un controlador que construye `Filters` o `Update` a mano: eso es del servicio.
- Un servicio que construye un `ResponseDTO`.

**No es hallazgo**
- Un servicio que devuelve `*domain.X` cuando el caso de uso devuelve la entidad tal cual: es
  explicitamente legal. El `Result` existe para cuando la salida combina fuentes.

**Fix**: di el tipo que falta y donde va — `ports/contracts/<caso>.go` con `XQuery`/`XResult`, y
el mapper del controlador que lo traduce.

---

## 3. Vocabulario de tipos

El vocabulario — que significa cada sufijo y en que carpeta vive — esta en la seccion 4.1 de la
skill, con sus reglas de decision.

**MEDIUM**
- Un tipo con el sufijo equivocado para lo que hace: un `ListXCommand` que solo lee, un
  `GetXResult` que vive en `persistence/`, un `XFilters` en `contracts/`.
- Un archivo en la carpeta equivocada aunque el nombre sea correcto.

**LOW / NIT**
- Nombre de archivo que no sigue `snake_case` o que nombra la capa en vez del caso de uso
  (`service_impl.go` en vez de `list_leads.go`).

---

## 4. Estructura del modulo

La estructura canonica del modulo esta en la seccion 3 de la skill.

**MEDIUM**
- Caso de uso nuevo metido dentro de un archivo de otro caso de uso: la regla es **un archivo por
  caso de uso** (`list_leads.go`, `update_lead.go`).
- `<mod>_service.go` con logica dentro: solo lleva el struct y el constructor.
- Carpeta nueva con un nombre fuera del juego (`usecases/`, `handlers/`, `models/`, `repo/`).
- Modulo nuevo sin `ports/service.go` o sin `ports/repository.go`.

**LOW**
- Paquete cuyo nombre no es `<capa>_<modulo>` o no coincide con la carpeta
  (`ports_lead` en `ports/persistence/`, que deberia ser `ports_lead_persistence`).
  Es una desviacion **conocida** del repo actual: en codigo existente que el diff no toca, no es
  hallazgo; en un modulo nuevo, si.

---

## 5. Dominio y value objects

**HIGH**
- Entidad de dominio con campos primitivos (`string`, `int64`) donde el modulo ya tiene un value
  object para ese concepto, o con struct tags (`json:`, `db:`). El dominio no se serializa.
- Value object sin constructor validador: si `NewX` no comprueba nada, el tipo no aporta nada y
  cualquiera puede construir uno invalido.

**MEDIUM**
- Validacion de un concepto repetida en el servicio en vez de vivir en el constructor del VO.
- Concepto compartido (`ID`, `Email`, `Phone`, `State`) duplicado en un modulo teniendo uno en
  `core/shared/domain`.

**Fix**: el VO concreto y su archivo — `core/shared/domain/email.go` ya existe, usalo — no
"crear un value object".

---

## 6. Controladores (y handlers de worker)

En un worker, todo lo de aqui aplica al handler de `driving/`, salvo lo que es literalmente HTTP
(rutas, `http_response`, `c.Context()`). La regla que si aplica siempre: **el tipo del evento no
entra al servicio**; se traduce a Command/Query en el handler.

**BLOCKER**
- Endpoint nuevo que expone datos de investor, lead, pagos o creditos registrado en
  `public.routes.go` en vez de `private.routes.go`. No es una decision de routing: es una fuga.

**HIGH**
- Entrada usada sin parsear a `RequestDTO` y **sin validar** antes de convertirse en Command/Query.
- `context.Background()` o `context.TODO()` en un camino de request: hay que pasar `c.Context()`.
  Una query que no se cancela sobrevive al request que la pidio.
- El controlador tiene el struct concreto del servicio o un repositorio en vez de `ports.XService`.

**MEDIUM**
- Respuesta escrita a mano (`c.JSON`, `fiber.Map`) en vez de `http_response.*`: rompe el contrato
  de respuesta que el frontend ya consume.
- Logica de negocio en el controlador — un `if` que un experto del dominio reconoceria como regla.

**No es hallazgo**
- `*fiber.Ctx` en el controlador: es su sitio, y el unico.

---

## 7. Adaptadores y SQL

**BLOCKER**
- Valor interpolado en la query (`fmt.Sprintf`, concatenacion de una variable). Es inyeccion.

**No es hallazgo**: concatenar **fragmentos fijos** — columnas del `SELECT`, `JOIN`s, `ORDER BY`
resuelto por un `switch` sobre constantes tipadas. Es el patron del repo y es correcto: lo que
nunca se concatena es un **valor**. Antes de reportar, comprueba de donde sale el string: si viene
de un `const` o de un `switch` exhaustivo, no hay hallazgo.

**HIGH**
- `rows.Close()` faltante, o `rows.Err()` sin comprobar tras el bucle.
- Query dentro de un `for` sobre el resultado de otra query (N+1).
- `sql.ErrNoRows` tratado como fallo generico donde es un resultado normal.

**MEDIUM**
- Constructor del repositorio que devuelve el struct en vez del puerto.
- Adapter que conoce Commands/Queries/Results.

---

## 8. Servicios

**HIGH**
- El struct del servicio es **exportado**, o el constructor devuelve el struct en vez de la
  interfaz del puerto. Rompe la sustitucion por mocks y filtra la implementacion.
- `ctx` no propagado a alguna llamada de repositorio o de cliente.

**MEDIUM**
- Un caso de uso nuevo que no se declara en `ports/service.go`.
- Servicio que orquesta llamando a `adapters` directamente en vez de a traves del puerto.

---

## 9. Inyeccion de dependencias

**HIGH**
- `NewMySQLXRepository` o `NewXService` llamados fuera de `internal/container`. La raiz de
  composicion es una sola; si se construyen servicios por ahi, nadie sabe que comparte que.
- `os.Getenv` fuera de `internal/config`.

**MEDIUM**
- Controlador registrado en rutas sin pasar por el container.
- Singleton de paquete o `init()` haciendo cableado.

---

## 10. Estandares Go

Lo que aqui no este, lo cubre la seccion Golang de `stacks.md`. Aqui solo lo que este equipo ha
escrito como ley:

**HIGH**
- Error ignorado (`_ =`) en un camino que puede fallar.
- Error envuelto con `%v` en vez de `%w` al cruzar una capa: rompe `errors.Is`/`errors.As`.
- Token, contraseña, cadena de conexion o SQL con valores dentro de un mensaje de error o de log.
- `panic` en un camino de request.

**MEDIUM**
- Codigo de estado HTTP decidido dentro del servicio en vez de en el controlador.
- El mismo error logueado en cada capa: cinco lineas para un solo fallo.
- `log.Printf` / `fmt.Println` donde toca `slog` — coordinalo con `convention-reviewer`, que ya
  marca el debug olvidado; si el ya lo reporta, no dupliques.

**LOW / NIT**
- Exportado sin comentario de doc que empiece por su nombre.
- `interface{}` en vez de `any`.
- Slice que se va a llenar sin pre-dimensionar (`make([]*X, 0, len(src))`).

---

## Proyectos a medio migrar

La regla 1 del contrato de hallazgos manda: **el diff decide que reportas**.

- Codigo preexistente que el diff no toca → no es hallazgo, por mucho que viole la ley.
- Codigo **nuevo** que copia el patron viejo → si lo es.
- Las desviaciones conocidas que la propia skill lista (nombres de paquete en `persistence/`, el
  typo `ports_investor_refres_token`, `log.Printf` en `controllers/lead.go`) no se reportan en
  codigo existente: ya estan documentadas y su arreglo es su propio PR.
- Si el modulo entero esta fuera de arquitectura y migrarlo excede el PR: **un solo** hallazgo
  `MEDIUM` con todas las `ubicaciones` y `decision_externa: "producto"`.

Nunca conviertas esta revision en una auditoria del repo. Si el diff añade un caso de uso, el
informe habla de ese caso de uso.
