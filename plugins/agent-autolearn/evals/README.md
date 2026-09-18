# Evaluaciones con bugs conocidos

Sirven para comprobar que un cambio en el plugin (prompts, leyes, routing, modelos) **no pierde
calidad** y para comparar su coste sobre exactamente el mismo codigo. Preparar y puntuar es Python
determinista y no gasta tokens; lo unico que gasta es la revision real del fixture, que solo se
lanza a mano. No forman parte de `/pre-pr-review` ni se ejecutan solas.

| Caso | Stack | Bugs sembrados | Senuelo |
|---|---|---|---|
| `nextjs-leads` | Next.js 15 | inyeccion SQL, Server Action sin authz, insert sin await, ultima pagina perdida, modulo server-only en Client Component | query parametrizada |
| `go-orders` | Go hexagonal | inyeccion SQL, error de query ignorado, dominio importa adaptador, escritura concurrente en map | query parametrizada |

## Uso

```bash
S=plugins/agent-autolearn/scripts/eval_review.py
python3 $S setup --case go-orders --dest /tmp/eval-go        # repo nuevo: development + feature/eval
# En Claude Code, dentro de /tmp/eval-go:  /pre-pr-review feature/eval
python3 $S score --case go-orders --repo /tmp/eval-go --out go-2.12.0.json
# ...cambia el plugin, repite setup/revision en otro directorio y puntua a go-2.13.0.json
python3 $S compare go-2.12.0.json go-2.13.0.json             # exit 1 si se pierde calidad
```

`score` lee el ledger y el resumen de tokens compartido del fixture. Devuelve `recall`, bugs
`missed`, `severity_underrated` (detectado por debajo de `min_severity`), `decoy_hits` (falsos
positivos seguros), `unexpected` (hallazgos no sembrados: los juzga una persona, no son falsos
positivos por definicion), descartes del agregador y tokens observados/orquestador.
`compare` falla si desaparece un bug antes detectado, aumentan los senuelos reportados o baja una
severidad por debajo del minimo. Menos tokens con la misma deteccion es una mejora; menos tokens con
un bug perdido no lo es. Una sola corrida por version es una muestra: repite antes de concluir por
una diferencia pequena.

## expected.json

`bugs[]` y `decoys[]`: `id`, `file`, y `symbols` y/o `lines: [desde, hasta]`; `keywords` opcionales
(basta una, en categoria/titulo/evidencia/why) separan dos bugs del mismo simbolo; `min_severity`
y `why` documentan el bug. Un hallazgo cuenta si coincide el archivo, el simbolo o la linea, y una
keyword. `base/` es development; `change/` se superpone en `feature/eval`.
Un caso nuevo debe compilar salvo que el fallo de build sea el bug sembrado.
