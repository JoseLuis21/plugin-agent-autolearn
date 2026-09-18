# Guia por stack

Detecta el stack por los archivos tocados en el diff y aplica solo las secciones relevantes.

| Señal | Stack |
|---|---|
| `next.config.*`, `app/`, `pages/`, `.tsx` | Next.js |
| `go.mod`, `*.go` | Golang |
| `application/controllers/`, `application/models/`, `system/`, `*.php` | PHP CodeIgniter |
| `serverless.yml`, `template.yaml`, `handler.*`, `sam/`, `cdk/` | AWS Lambda |

---

## Next.js

> **Arquitectura obligatoria del equipo:** el DAL en `_internal`, los Server Actions finos, Zod en
> las dos fronteras y los DTOs los verifica `nextjs-architecture-reviewer` con su propio criterio en
> `nextjs-architecture.md`. Lo de aqui abajo es la guia general del
> framework, para el resto de revisores.

- **Frontera server/client.** `"use client"` en un archivo que importa secretos o SDK de servidor = filtracion al bundle. Toda variable sin prefijo `NEXT_PUBLIC_` usada en un componente cliente es un bug.
- **Frontera que no compila.** Si desde un `"use client"` se llega —por el import directo o por el
  de otro componente— a un modulo con `import "server-only"`, `next/headers` o un builtin de Node,
  el build de produccion falla. Lo detecta el escaner de la corrida
  (`_client-server-raw.json`) y lo confirma `nextjs-architecture-reviewer`; si ese revisor no
  corre, mira tu la cadena antes de dar el PR por bueno. `"use server"` en el modulo importado
  corta la cadena: eso si compila.
- **Server Actions.** Son endpoints publicos. Sin validacion de entrada (zod/valibot) y sin chequeo de sesion, cualquiera los invoca.
- **Cache y revalidacion.** `fetch` sin `cache`/`revalidate` explicito en datos por usuario sirve datos de otro usuario. Revisa `revalidatePath`/`revalidateTag` faltantes tras una mutacion.
- **Cache: comprueba la version mayor antes de afirmar nada.** Lee `next` en el lockfile. En Next 14
  `export const dynamic = "force-dynamic"` implicaba `fetchCache: "force-no-store"` y anulaba el cache
  de los `fetch` de la ruta; **a partir de Next 15 ya no**: la ruta se renderiza por request, pero cada
  `fetch` conserva su propio `cache`/`next.revalidate`. Afirmar lo contrario en un proyecto moderno es
  un falso positivo, y el "fix" de añadir `export const revalidate` no cambia nada cuando el fetch trae
  su propio `revalidate`. Si vas a reportar coste de cache, dilo con numeros (cuantas llamadas por hit)
  y di como lo mediste.
- **Bloque `nextjs-agent-rules` en `AGENTS.md`.** Lo escribe y reinserta `next dev`
  (`node_modules/next/dist/server/lib/generate-agent-files.js`), entre marcadores
  `BEGIN:`/`END:nextjs-agent-rules`. No es hallazgo: borrarlo solo genera un cambio sin commitear que
  vuelve solo.
- **Route handlers.** `params`/`searchParams` son strings sin tipar; el tipo TypeScript no valida en runtime.
- **Hooks.** Dependencias faltantes en `useEffect`, estado derivado que deberia ser calculado, fetch sin `AbortController` en efectos que se re-disparan.
- **Hidratacion.** `Date.now()`, `Math.random()`, `localStorage` en render → mismatch servidor/cliente.

## Golang

> **Arquitectura hexagonal del equipo:** en los repos con `internal/core/`, la regla de
> dependencias, el flujo DTO/Command/Query/Result/Filters y el container los verifica
> `go-architecture-reviewer` con su propio criterio en `go-architecture.md`. Lo de aqui
> abajo es la guia general del lenguaje, para el resto de revisores.

- **Errores.** Todo error debe manejarse o envolverse con `%w`. Un `err` ignorado con `_` en el diff es hallazgo.
- **Concurrencia.** Goroutines sin cancelacion por `context`, variables de loop capturadas en closures (pre Go 1.22), maps compartidos sin mutex, `WaitGroup.Add` dentro de la goroutine.
- **Recursos.** `defer rows.Close()`, `defer resp.Body.Close()`, `defer f.Close()` faltantes. `defer` dentro de un loop acumula hasta el retorno.
- **Nil.** Punteros e interfaces: una interfaz con puntero nil adentro **no** es `== nil`.
- **SQL.** `fmt.Sprintf` en una query es inyeccion. Usa placeholders.
- **Slices.** `append` sobre un slice compartido puede pisar el array subyacente.

## PHP CodeIgniter

- **Inyeccion.** `$this->db->query()` con interpolacion. Usa query bindings o el query builder. `$this->db->where()` con string crudo tampoco escapa.
- **Entrada.** `$_GET`/`$_POST` directos en vez de `$this->input->get/post()` con XSS filter. Sin validacion via `form_validation`.
- **XSS.** `echo` de datos de usuario sin `html_escape()` en las vistas.
- **CSRF.** Endpoint POST nuevo con `csrf_protection` desactivado o formulario sin `form_open()`.
- **Autorizacion.** Controlador nuevo sin el chequeo de sesion/rol que usan sus hermanos en la misma carpeta. Compara siempre contra los controladores existentes.
- **Rutas.** Metodos publicos del controlador son accesibles por URL. Un helper publico = endpoint expuesto.
- **N+1.** Query dentro de un `foreach` sobre resultados de otra query.

## AWS Lambda

- **Idempotencia.** SQS/EventBridge/S3 entregan al menos una vez. Un handler que cobra, envia mail o inserta sin clave de idempotencia duplica en produccion.
- **Cold start y estado global.** Variables fuera del handler persisten entre invocaciones: conexiones DB (bien), pero tambien estado por usuario cacheado por error (fuga entre clientes).
- **Timeouts.** Timeout del handler mayor al del API Gateway (29s) o menor al de la llamada externa que hace. Reintentos sin backoff.
- **Errores parciales.** Batch de SQS: si el handler lanza, se reprocesa el lote entero. Sin `batchItemFailures` se reprocesan los mensajes ya exitosos.
- **Permisos.** IAM con `*` en Action o Resource en el diff de infra.
- **Secretos.** Credenciales en variables de entorno en texto plano en vez de Secrets Manager/SSM.
- **Conexiones.** Pool de DB creado dentro del handler = agota conexiones bajo concurrencia.
