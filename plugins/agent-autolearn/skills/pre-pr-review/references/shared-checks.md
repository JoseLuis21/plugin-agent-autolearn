# Comprobaciones compartidas sin delegar el juicio

`run.check_owner` es el unico escritor de `shared-checks.json`, preparado con tree/head y checks
vacios. Ejecuta los comandos documentados de build/tests relevantes como parte de su revision
normal, antes de lanzar a los demas revisores. No instales dependencias ni ejecutes servicios,
migraciones o suites externas solo para llenar este archivo. Si no hay un comando aplicable,
registra el motivo. No compartas secretos ni valores de entorno sensibles.

Publica atomicamente (archivo temporal + rename), conservando tree/head/owner:

```json
{
  "schema_version": 1,
  "tree": "<run.tree>", "head": "<run.head>", "owner": "test-reviewer",
  "status": "complete",
  "checks": [{
    "command": ["go", "build", "./..."], "cwd": "src",
    "scope": "Compilacion del modulo; no ejecuta tests",
    "environment": "Entorno local heredado, sin overrides",
    "status": "passed", "exit_code": 0,
    "evidence": "Compilo sin errores", "log": "checks/build.log"
  }]
}
```

Si no hay ningun comando aplicable, publica `"status": "complete"`, `"checks": []` y un `"reason"`
con el motivo. En comandos de tests añade `"tests_run": <n>` al check.

`complete` significa que el dueño termino de registrar, no que los checks pasaron. Cada check
usa `passed`, `failed`, `unavailable` o `not_run`, con exit_code real o null si no termino.
Guarda la salida completa en RUN_DIR/checks y un resumen concreto en evidence; log puede ser null
si no hubo ejecucion. Timeout o dependencia ausente se explican, nunca se convierten en passed.
En tests, indica cuantos se ejecutaron; salida con cero tests no demuestra cobertura.

`scripts/shared_checks.py --run-dir` valida el archivo sin ejecutar nada: tree/head/owner iguales a
run.json, `passed` solo con exit_code 0, `failed` nunca con 0, log existente dentro de RUN_DIR para
lo ejecutado, y `passed` con `tests_run: 0` rechazado. Escribe `validation` en el mismo archivo.
Con `validation.reusable: false` trata el archivo como sin evidencia compartida. Un check `failed`
bien registrado es un hecho valido y reutilizable. La validacion se informa en
`clasificacion.resumen.shared_checks`; nunca cambia el veredicto ni la cobertura.

Los consumidores reutilizan un resultado solo si tree/head coinciden con run.json y el comando,
cwd, entorno y alcance responden a su pregunta. No uses resultados de otra corrida ni de una
copia del codigo como prueba del snapshot actual. Codigo cambiado exige preparar otra corrida;
ledger.py sigue comprobando el snapshot al finalizar. Ausencia, pending o JSON invalido significa
sin evidencia compartida, no revision limpia. Declara la limitacion o ejecuta la comprobacion
necesaria y registra el resultado en tu searches/<reviewer>.json, sin sobrescribir el archivo comun.

No repitas el mismo build/test para obtener el mismo dato. Si necesitas otro escenario, flags,
entorno, test de regresion o investigar un resultado dudoso, ejecutalo y registra que aporta.
La compilacion y los tests compartidos no sustituyen leer funciones, seguir callers ni verificar
HIGH/BLOCKER independientemente. Los hallazgos y sus decisiones siguen en los JSON de cada revisor.
