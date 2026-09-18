# Calibracion de revision — arquitectura Next.js

**La ley esta en `${CLAUDE_PLUGIN_ROOT}/skills/development-nextjs/SKILL.md`.** Este archivo no
la repite: dice **con que severidad** pesa cada violacion y, sobre todo, **que no es hallazgo** —
que es donde un revisor hace daño.

Lee la skill primero. Sin ella, esto son severidades sin reglas.

---

## Fuente de verdad

La skill del plugin es la ley por defecto. Si el repo revisado documenta la suya — su propia copia
de `development-nextjs`, `AGENTS.md`, `CLAUDE.md` — **esa manda**: leela antes de juzgar y
respeta sus adaptaciones y sus deudas reconocidas. Donde difieran, gana la del repo y lo anotas en
`sin_hallazgos_en`.

Que un proyecto no traiga su propia copia no es hallazgo: la ley viaja en el plugin.

---

## 1. Frontera cliente / servidor

Es la regla que mas caro sale: lo que cruza mal acaba en el bundle del navegador — o ni siquiera
llega a compilar.

### 1.a Modulo de servidor en el grafo cliente — no compila

Un archivo con `"use client"` que importa, **directa o transitivamente**, un modulo con
`import "server-only"`, `next/headers` o un builtin de Node, **rompe el build**:

```
Error: You're importing a module that depends on "server-only" into a React Client
Component module. [...] 'server-only' cannot be imported from a Client Component module
```

Esto no se discute ni se calibra: es **BLOCKER** siempre, con `confidence: alta`. No es una
opinion de arquitectura sino un fallo de compilacion, y llega hasta produccion porque el build
de Vercel/Cloudflare es lo primero que se lo encuentra, no la revision.

La preparacion resuelve el grafo de imports y deja las cadenas en
**`$RUN_DIR/_client-server-raw.json`**: `chain` va del Client Component mas cercano hasta el
modulo roto, con el `marker` que lo delata y la linea. Son **candidatos**: abre los archivos de
la cadena y confirma que las tres cosas siguen siendo ciertas antes de reportar —
la directiva `"use client"`, el import intermedio y el marcador. Con `en_delta: false`, la cadena
ya existia y el diff no la toca: aplica la regla 1 y no la reportes como hallazgo del PR.

**Lo que corta la cadena y no es hallazgo:**
- `"use server"` en el modulo importado: es un Server Action, Next serializa la llamada en vez de
  empaquetar el modulo. El escaner ya no lo sigue.
- `import type { … }`: TypeScript lo borra al compilar y no llega al bundle.

**El fix casi siempre es uno de estos dos**, y hay que elegir explicitamente:
1. El cliente necesita **invocar** la operacion → `"use server"` en el archivo del action, que
   solo exporte funciones async, y el trabajo de servidor detras en `_internal`.
2. El cliente solo necesita **el dato** → sacarlo del cliente: que el Server Component lo pida
   al DAL y lo pase como DTO por props.

Un `"use server"` puesto encima de un archivo que tambien exporta tipos, constantes o helpers no
compila: ese archivo solo puede exportar funciones async. Dilo en el `fix` cuando sea el caso.

### 1.b El resto de la frontera

**BLOCKER**
- Un archivo con `"use client"` (o importado solo desde uno) que importa de `_internal`.
- Un token, `session`, `accessToken` o un registro crudo del backend pasado como prop a un
  Client Component, o serializado en el HTML.
- Variable de entorno privada (sin `NEXT_PUBLIC_`) leida en codigo que llega al cliente.

**HIGH**
- Llamada `fetch` a la API de Go desde un Client Component, aunque vaya sin token.
- Archivo de `_internal` que accede a datos privados **sin** `import "server-only"`. Sin esa
  linea nada impide que mañana alguien lo importe desde el cliente y el fallo sea silencioso.

**No es hallazgo**
- `NEXT_PUBLIC_*` en cliente: para eso existe el prefijo.
- Un Server Component que importa `_internal`: es exactamente lo correcto.
- Un tipo o interfaz importado desde `_interfaces/` por un componente cliente.

**Fix**: mover la lectura de datos a `_internal`, exponer un DTO, y pasar el DTO. Si el cliente
necesita disparar la accion, un Server Action en `_actions/` que delega en el DAL.

---

## 2. `_internal` es el unico DAL

Dentro de `_internal` va — y **solo ahi**: llamadas al backend externo, `process.env` privado,
wrappers de fetch autenticado, chequeos de autorizacion, mapeo `snake_case` → `camelCase`,
creacion de DTOs, reglas de negocio de servidor y tipos privados de la respuesta del backend.

**HIGH**
- `fetch` a la API de Go escrito fuera de `_internal` (en un `page.tsx`, en un `_actions/*`, en un
  route handler que no delega).
- `process.env.<PRIVADO>` fuera de `_internal`.
- Headers de autenticacion (`Authorization: Bearer …`) montados a mano fuera del wrapper central.

**MEDIUM**
- Logica de negocio de servidor que vive en el componente en vez del DAL.

El contrato del wrapper central — base en `NEXT_PUBLIC_API_URL`, `Authorization` y `Content-Type`
obligatorios — esta en la seccion 5 de la skill. Un `fetch` nuevo que monta esos headers por su
cuenta es hallazgo aunque funcione: el dia que cambie la autenticacion hay que cazarlos uno a uno.

**Fix**: nombra el archivo destino concreto (`app/<modulo>/_internal/<recurso>.dal.ts`) y la
funcion que debe quedar expuesta, no "mover al DAL".

---

## 3. Server Actions son controladores finos

Un Server Action puede: validar con Zod, llamar al DAL, devolver un estado seguro,
`revalidatePath`/`revalidateTag`, redirigir. Nada mas.

**BLOCKER**
- Server Action que lee o muta datos privados **sin** validar entrada y **sin** que el DAL
  reverifique sesion. Un Server Action es un endpoint publico: quien conozca el id lo invoca.

**HIGH**
- `fetch` crudo al backend dentro del action.
- `process.env` privado dentro del action.
- El action devuelve la respuesta cruda del backend.
- Falta `"use server"` donde se espera un action, o esta en un archivo que tambien exporta
  helpers no-action. Si ademas un Client Component importa ese archivo, sube a **BLOCKER**: es
  el caso 1.a y no compila.

**MEDIUM**
- Autorizacion duplicada en el action y en el DAL (dos sitios que divergen; el DAL es el que manda).
- Mutacion sin `revalidatePath`/`revalidateTag`, con una vista que va a quedar desactualizada.

**Fix**: el esquema Zod concreto en `_schemas/<accion>.schema.ts` y la funcion del DAL que debe
llamar.

---

## 4. Zod en las dos fronteras

Se valida lo que **entra del cliente** y lo que **entra del backend**. Las dos.

**HIGH**
- `formData`, body JSON, `params`, `searchParams` o entrada de un action usados sin parsear.
- `await response.json()` usado directamente, sin esquema. El tipo de TypeScript no valida en
  runtime: el dia que el backend cambie un campo, el fallo aparece tres capas mas arriba.

**No es hallazgo**
- Datos que ya llegan parseados por un helper del propio repo — compruebalo antes de reportar.
- Un endpoint publico sin datos sensibles cuyo payload ya valida el framework.

---

## 5. DTOs

**BLOCKER**
- El DTO arrastra `password_hash`, `access_token`, secretos o datos de otros usuarios.

**HIGH**
- Respuesta cruda del backend pasada a un componente (`<UserProfile user={rawUser} />`).
- `snake_case` cruzando al frontend: si el componente lee `created_at`, no hay mapper.

**LOW**
- DTO que devuelve mas campos de los que la UI usa, sin que ninguno sea sensible.

**Fix**: el mapper concreto en `_internal/<recurso>.mapper.ts` y la lista de campos que sobreviven.

---

## 6. Auth y authz viven en el DAL

**BLOCKER**
- Funcion del DAL que lee o muta datos privados sin comprobar sesion.
- Mutacion protegida **solo** por un `redirect('/login')` a nivel de pagina: el Server Action sigue
  siendo invocable sin pasar por esa pagina.
- Autorizacion por rol ausente donde el recurso lo exige (borrar, facturar, ver datos de otro).

**HIGH**
- Chequeo de sesion presente pero no de permiso sobre **ese** recurso (`users.delete`, dueño del
  registro). Autenticado no es autorizado.

**Fix**: cita el patron que ya usa el repo (`getAccessTokenOrThrow`, `session.user.permissions`)
en vez de inventar uno nuevo.

---

## 7. Estructura de carpetas

Las carpetas canonicas — globales con `_` delante, y las del modulo en `app/<modulo>/` — estan en
las secciones 2 y 4 de la skill. Contrasta contra esa lista, no contra tu memoria.

**MEDIUM**
- Carpeta compartida nueva sin `_`.
- Modulo nuevo sin `error.tsx`, o sin la carpeta que su contenido exige (tiene actions pero no
  `_actions/`).
- Archivo colocado fuera de su carpeta: un esquema Zod suelto en `_components/`, un componente de
  un solo modulo en `_components/` global.

**LOW / NIT**
- Nombre de archivo fuera de convencion (`users.dal.ts`, `users.mapper.ts`, `create-user.action.ts`,
  `create-user.schema.ts`).

Un modulo **preexistente** que no sigue la estructura y que el diff solo toca de refilon no es
hallazgo del PR: ver "Proyectos que aun no siguen la ley".

---

## 8. Zustand

**MEDIUM**
- Store que necesita sobrevivir al refresco sin middleware `persist`.
- Componente que lee un store persistido y se renderiza en SSR sin guarda de hidratacion
  (`useHasHydrated`) → mismatch servidor/cliente.
- Estado de servidor metido en un store de cliente: datos que ya vienen del DAL duplicados en
  Zustand y consultados por el cliente otra vez.

---

## 9. Cache

**HIGH**
- Dato privado de usuario cacheado de forma global o compartida entre requests.
- Token o sesion cacheado.

**MEDIUM**
- Lectura repetida en el mismo request sin `cache()` de React donde el repo ya lo usa.
- Mutacion sin revalidacion (ver seccion 3).

Antes de afirmar comportamiento de cache, **lee la version de `next` en el lockfile**: las reglas
cambiaron entre 14 y 15 (regla 8 del contrato de hallazgos). Si no puedes verificarlo, `confidence: baja`
o no lo reportes.

---

## 10. Errores

**MEDIUM**
- Error del DAL que filtra token, URL interna o detalle del backend en el mensaje.
- Modulo sin `error.tsx` que puede lanzar desde el Server Component.

**LOW**
- Mensaje de error generico donde el usuario no puede saber que hacer.

---

## 11. Stack

**HIGH**
- `package-lock.json`, `yarn.lock` o `bun.lockb` en el diff: el gestor es **pnpm**, y punto. Dos
  lockfiles en el repo son dos arboles de dependencias distintos.

**MEDIUM**
- Componente de UI escrito a mano donde shadcn ya da uno equivalente y el repo ya lo usa.
- Auth montada al margen de AuthJS en un proyecto que ya lo tiene configurado.

---

## Proyectos que aun no siguen la ley

Muchos repos son anteriores a la skill. Ahi la regla 1 del contrato de hallazgos manda: **el diff
decide que reportas**.

- Codigo preexistente que el diff no toca → no es hallazgo.
- Codigo **nuevo** que repite el patron viejo → si es hallazgo: la ley aplica a lo que se escribe hoy.
- Si el modulo entero esta fuera de arquitectura y migrarlo excede el PR: **un solo** hallazgo
  `MEDIUM` con todas las `ubicaciones`, marcado `decision_externa: "producto"`, explicando que la
  migracion es su propio PR. No treinta hallazgos repitiendo lo mismo.

Nunca conviertas esta revision en una auditoria del repo completo. Si el diff añade dos archivos,
el informe habla de esos dos archivos y, como mucho, del patron que arrastran.
