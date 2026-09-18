# Reglas de hallazgo — el porque y los casos limite

Apendice de `findings-contract.md`. Ahi estan el esquema y las once reglas en una linea; aqui la
prosa de las que se aplican mal cuando se enuncian solas. **Abre solo la seccion que necesites**:
esto no se lee entero por defecto.

## Regla 1 — Delta para hallazgos nuevos, identidad para pendientes

Si el problema ya existia antes del cambio y el diff no lo toca, no es hallazgo — salvo que el
cambio lo agrave. Pero una vez que algo **si** es hallazgo, su alcance no termina en el diff:
ver la regla 9. Los pendientes historicos se revalidan por huella aun fuera del delta; nunca
se cierran por ausencia en el patch.

## Regla 5 — Ningun hallazgo viaja sin su solucion

Describir el problema es la mitad del trabajo; la otra mitad es decir que hacer. Aplica igual al
LOW mas pequeño que al BLOCKER: si no sabes como se arregla, o no lo has entendido lo bastante
para reportarlo, o es una pregunta y no un hallazgo — marcalo `decision_externa` y di quien tiene
la respuesta.

El `fix` va **explicito y separable**, no diluido en la ultima frase del parrafo: quien lo lee
tiene que poder copiarlo sin releer el diagnostico.

## Regla 7 — Nada generado ni vendorizado

Si el bloque lo escribe una herramienta y lo reinserta sola, borrarlo no es un fix. Antes de
reportar contenido de `AGENTS.md`, `CLAUDE.md`, lockfiles, migraciones autogeneradas o cualquier
archivo con marcadores `BEGIN:`/`END:`, comprueba quien lo genera. Si lo regenera una dependencia,
o no es hallazgo, o es `decision_externa`.

## Regla 8 — Nada de comportamiento de framework de memoria

Las reglas de cache, params y rendering cambian entre versiones mayores. Lee la version instalada
en el lockfile antes de explicar por que algo se comporta de una forma, y dilo en el `why`. Si no
puedes verificarlo, baja la `confidence` a `baja` o no lo reportes.

## Regla 9 — Arregla la ventana, consulta el impacto necesario

Completa las apariciones dentro de la ventana. Antes de buscar, reutiliza searches.json y los
resultados de otros revisores. Una consulta por simbolo/patron esta justificada si resuelve una
duda concreta de callers, contratos o causa raiz; documenta query, alcance y resultado para que
el agregador no la repita. No hagas un barrido general por cada finding.

Los callers que el cambio rompe siguen siendo evidencia relevante aunque esten fuera del diff.
Las copias preexistentes no agravadas van a **PR de seguimiento**, con una muestra identificada;
no hace falta enumerar todos los usos del repo. El fix inmediato cubre las ubicaciones de la
ventana y el impacto causado por este cambio. No pidas limpiar deuda ajena para cerrar la pasada.

Esto no es automaticamente decision_externa: seguimiento significa otro alcance; decision externa
significa que otra persona/rol decide. Nunca amplias la rama por exigencia de un censo repetido.

## Regla 10 — Dos sitios que definen lo mismo: uno sobrevive

Cuando el defecto es "A y B describen la misma cosa y han divergido" (dos globs de tests, dos
listas de rutas publicas, un enum duplicado en front y back, un umbral repetido), proponer "poner B
igual que A" solo aplaza la proxima divergencia: el dia que alguien mueva algo, vuelven a separarse
y nadie se entera. El `fix` tiene que decir **cual de los dos sobrevive y como el otro pasa a
derivarse de el** — exportar el valor y consumirlo, generar uno desde el otro, o fusionar los dos
pasos en uno solo.

Redactalo asi de explicito, porque un lector con prisa se queda con "hay que tocar A" y elige mover
B en la direccion contraria. Y si el defecto es que un control no detecta el caso para el que se
escribio, pide **la prueba que lo demuestre**: "un fixture con X renombrado debe dar exit 1". Sin
ese caso, la correccion no es verificable y vuelve a romperse en silencio.

## Regla 11 — El valor real de la config, no el fallback del codigo

`parseInt(process.env.X || "0", 10)` no significa que `X` valga 0: significa que vale 0 **si nadie
la define**. Antes de razonar sobre ese numero, busca el valor efectivo — `.env`, `.env.*`,
`docker-compose.yml`, `Dockerfile`, `buildspec*`, manifiestos de despliegue — y citalo en el `why`
con el archivo donde lo leiste.

Si el valor vive en infraestructura que no puedes ver (task definition de ECS, secretos del CI),
dilo explicitamente, reporta con `confidence: baja` y enuncia la condicion: "si `X` no esta
definida en produccion, entonces...". Nunca presentes el fallback como si fuera la configuracion.

## `decision_externa` — hallazgos que el autor no puede cerrar

Marca `"decision_externa"` con el rol que decide cuando el arreglo **no esta al alcance de quien
abre el PR**: cambia infraestructura, toca un archivo marcado como no modificable, necesita una
dependencia nueva, requiere rollout por fases, o la respuesta es de negocio y no de codigo.

Sigue poniendo la `severity` real — describe el riesgo, no quien lo arregla — pero el agregador los
saca del conteo y los lista aparte, para que no arrastren el veredicto ni reaparezcan como deberes
del autor en cada corrida.

Ejemplos: `X-Frame-Options` que depende de si marketing embebe el sitio; un `buildspec` marcado
`## DO NOT MODIFY ##`; una CSP que necesita `Report-Only` antes de aplicar; un refactor que toca 36
funciones fuera del diff.
