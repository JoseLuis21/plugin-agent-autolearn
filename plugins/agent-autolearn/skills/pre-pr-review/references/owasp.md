# OWASP — cobertura obligatoria de la revision de seguridad

**Como se escribe el codigo seguro esta en `${CLAUDE_PLUGIN_ROOT}/skills/owasp-security/SKILL.md`**,
la misma skill que el equipo usa al programar. Este archivo no la repite: dice **que categorias son
de paso obligatorio** en cada revision, con que severidad pesan y que **no** es hallazgo.

Dos listas, segun donde caiga el diff:

- **OWASP Top 10 (2021)** — aplicacion web. Next.js, vistas PHP, cualquier cosa que llegue al navegador.
- **OWASP API Security Top 10 (2023)** — API. Go, lambdas, route handlers, Server Actions
  (son endpoints publicos aunque no lo parezcan).

Un PR full-stack pasa por las dos.

---

## El contrato: obligatorio vs oportunista

**No se persigue cumplimiento del 100%.** Una revision pre-PR mira un diff, no la aplicacion
entera; prometer cobertura completa de OWASP en cada PR es teatro y ademas miente.

Lo que si se exige:

| Nivel | Que significa |
|---|---|
| **Paso obligatorio** | Recorres la categoria **en cada revision**, toque o no el diff su superficie, y declaras el resultado en `sin_hallazgos_en`. Son las que matan: ejecucion, acceso a datos de otro, bypass de sesion. |
| **Solo si el diff lo toca** | La miras si el diff toca su superficie (config, dependencias, logs, cripto). Si no la toca, no la nombras: rellenar `sin_hallazgos_en` con categorias que no miraste es peor que no citarlas. |

### Paso obligatorio — siempre

| OWASP | Categoria | Que buscas en el diff |
|---|---|---|
| **A03 / API8** | **Inyeccion** | SQL, NoSQL, comandos, LDAP, plantillas. Valor de usuario concatenado en una query o en un `exec`. **La mas cara y la mas facil de confirmar: empieza por aqui.** |
| **A01 / API1 / API3 / API5** | **Control de acceso roto** | Endpoint nuevo sin chequeo de sesion; autenticado pero no autorizado sobre **ese** recurso (IDOR); rol comprobado solo en el front; ruta nueva en la lista publica |
| **A07 / API2** | **Fallos de autenticacion** | Sesion, tokens, refresh, 2FA, rate limit de login, expiracion, revocacion |
| **A02** | **Fallos criptograficos** | Secretos en el diff, datos sensibles sin cifrar, hash debil, aleatoriedad no criptografica para tokens |
| **A10 / API7** | **SSRF** | URL de destino que viene del request sin allowlist |

### Solo si el diff toca su superficie

| OWASP | Categoria | Superficie que la activa |
|---|---|---|
| A04 | Diseño inseguro | Flujo nuevo de negocio: pagos, creditos, disputas, cambio de propiedad |
| A05 / API8 | Mala configuracion | `next.config.*`, CORS, cabeceras, Docker, IAM, buckets, security groups |
| A06 | Componentes vulnerables | `package.json`, `go.mod`, lockfiles |
| A08 | Fallos de integridad | Deserializacion, webhooks sin firma, CI/CD, dependencias desde URL |
| A09 / API9 | Logging y monitorizacion | Logs nuevos, manejo de errores, endpoints sin trazas donde sus hermanos si las tienen |
| API4 | Consumo de recursos | Endpoint nuevo sin limite de paginacion, subida de archivos, operacion cara sin rate limit |
| API6 | Acceso a flujos de negocio | Automatizable a escala: registro, envio de leads, compra |
| API10 | Consumo inseguro de APIs | Respuesta de un tercero usada sin validar |

---

## Como se reporta

**Cada hallazgo lleva su categoria OWASP** en el campo `owasp` (`A03`, `API1`, o las dos si
aplica). Sirve para agrupar en el informe y para que el equipo vea patrones entre PRs.

**Ninguna categoria se reporta sin una linea concreta del diff detras.** Esto no cambia con
OWASP: "el PR deberia considerar A04" no es un hallazgo, es relleno. Si no puedes copiar la linea
que falla, no lo reportes — regla 3 del contrato de hallazgos.

**`sin_hallazgos_en` es la prueba de cobertura.** Las cinco obligatorias van ahi siempre, con lo
que miraste:

```json
"sin_hallazgos_en": [
  "A03 inyeccion: 3 queries nuevas en leads.dal.ts, las tres parametrizadas",
  "A01 control de acceso: el endpoint nuevo repite el guard de sus hermanos en el mismo grupo",
  "A07 autenticacion: el diff no toca sesion ni tokens",
  "A02 cripto: sin secretos ni hashing en el diff",
  "A10 SSRF: sin fetch a URL construida con entrada de usuario"
]
```

Una obligatoria que el diff no toca se declara asi — "el diff no toca X" — no se omite. La
diferencia entre "mirado y limpio" y "no mirado" es justo lo que el equipo necesita saber.

---

## Severidad

La marca el **impacto real**, no el numero de la categoria. Un A03 en una query interna sin datos
no es lo mismo que un A03 en el buscador publico.

| | |
|---|---|
| **BLOCKER** | RCE, inyeccion SQL explotable, bypass de autenticacion, exfiltracion de datos de otro usuario o tenant, secreto valido en el diff |
| **HIGH** | IDOR con condiciones, autorizacion que falta pero requiere una cuenta valida, XSS almacenado, SSRF a red interna, token con vida excesiva |
| **MEDIUM** | Falta de rate limit en algo caro, cabecera de seguridad ausente, log que filtra PII no critica, dependencia con CVE de severidad media |
| **LOW / NIT** | Endurecimiento sin vector demostrable hoy |

## Lo que NO es hallazgo

- Lo que el framework ya mitiga: escapado por defecto de React, query builder que parametriza,
  `html_escape()` en la vista. Compruebalo en el codigo antes de descartarlo, y tambien antes de
  reportarlo.
- Falta de rate limit en un endpoint interno detras del ALB, sin datos.
- Una cabecera de seguridad que depende de una decision de producto (si marketing embebe el sitio):
  eso es `decision_externa`, no un hallazgo del autor del PR.
- El clasico de una lista OWASP: "no se observa proteccion contra X" sin haber mirado si existe
  tres capas mas arriba.
