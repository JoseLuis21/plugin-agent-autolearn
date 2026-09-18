# Contrato de hallazgos (findings)

Todos los revisores escriben **un solo archivo JSON** en `$RUN_DIR/<slug>.json`.
El agregador lee solo los revisores asignados en run.json. No inventes campos ni cambies nombres.
Lee tambien tu pending/<slug>.json; cada huella asignada exige verificacion explicita.

```json
{
  "reviewer": "security-reviewer",
  "scope": "12 archivos, 340 lineas modificadas",
  "stacks_detectados": ["nextjs", "go"],
  "findings": [
    {
      "id": "SEC-01",
      "severity": "BLOCKER",
      "category": "sql-injection",
      "title": "Query construida por concatenacion en el endpoint de busqueda",
      "file": "app/api/search/route.ts",
      "symbol": "GET",
      "line": 42,
      "evidence": "const q = `SELECT * FROM leads WHERE name LIKE '%${term}%'`",
      "why": "term viene de searchParams sin sanitizar; permite exfiltrar la tabla completa.",
      "fix": "Usar consulta parametrizada: db.query('... LIKE ?', [`%${term}%`]).",
      "confidence": "alta",
      "owasp": "A03",
      "decision_externa": false,
      "ubicaciones": [
        "app/api/search/route.ts:42  (en la ventana)",
        "lib/reports/query.ts:15     (fuera de la ventana, mismo patron)"
      ]
    }
  ],
  "sin_hallazgos_en": ["autenticacion", "manejo de secretos"],
  "verifications": []
}
```

## Campos

| Campo | Obligatorio | Valores |
|---|---|---|
| `severity` | si | `BLOCKER`, `HIGH`, `MEDIUM`, `LOW`, `NIT` |
| `category` | si | slug corto en kebab-case (`n+1-query`, `race-condition`, `missing-test`) |
| `file` / `line` | si | ruta relativa al repo + linea del diff nuevo |
| `symbol` | si | la funcion, componente o handler que contiene el hallazgo (`getHelpsWithSlug`, `Play`, `GET`); si no hay ninguno, el nombre del archivo. **Es lo que da identidad al hallazgo entre pasadas**: la linea se mueve sola, el simbolo no |
| `evidence` | si | la linea real del codigo, copiada tal cual (max 3 lineas) |
| `why` | si | el fallo concreto: entrada/estado -> resultado incorrecto |
| `fix` | si, **en todas las severidades** | la accion concreta: que se escribe, donde y en lugar de que. Nunca "revisar esto", "considerar X" ni "mejorar el manejo". Un NIT sin fix no se reporta |
| `confidence` | si | `alta`, `media`, `baja` |
| `new_evidence` | solo al reabrir un descarte | Si `discarded.json` lista la misma categoria/archivo/simbolo: que cambio en el codigo o que prueba refuta el motivo del descarte. Sin eso, no lo reportes de nuevo |
| `ubicaciones` | si, si el patron se repite | todas las apariciones comprobadas dentro de la ventana; fuera, muestra para seguimiento y callers afectados por el cambio (regla 9) |
| `owasp` | solo `security-reviewer` | categoria OWASP del hallazgo: `A01`..`A10` (Top 10 2021), `API1`..`API10` (API Security 2023), o las dos separadas por coma |
| `decision_externa` | no | `false` (por defecto) o el rol que decide: `marketplace`, `infra`, `seguridad`, `producto`, `proveedor` |

## Escala de severidad

- **BLOCKER** — rompe produccion, expone datos, o corrompe estado. El PR no se sube.
- **HIGH** — bug real con camino de reproduccion claro, o hueco de seguridad explotable con condiciones.
- **MEDIUM** — falla en un caso plausible pero no comun; deuda que va a doler.
- **LOW** — mejora de robustez o claridad con impacto real.
- **NIT** — estilo o preferencia. Maximo 3 por revisor; si no aportan, cero.

## Reglas duras

Cada regla es completa tal como esta escrita: cumplela desde aqui. Las marcadas con **→ §N** tienen
ademas su porque y sus casos limite en `findings-rules.md`, en este mismo directorio, bajo el
encabezado `## Regla N`. Abre **solo esa seccion** cuando dudes de como aplicar la regla a un caso
concreto — no por costumbre, y nunca el archivo entero.

1. **El delta decide los hallazgos nuevos; los pendientes se revalidan por huella.** Lo que ya existia
   y el cambio no agrava no es hallazgo nuevo. No cierres un pendiente por quedar fuera del diff. → §1
2. **Verifica antes de reportar.** Lee el codigo alrededor de la linea antes de afirmar que falta
   algo: muchos "bugs" desaparecen cuando ves la validacion tres lineas arriba.
3. **Sin evidencia no hay hallazgo.** Si no puedes copiar la linea que falla, no lo reportes.
4. **Cero relleno.** Un archivo con `"findings": []` es un resultado valido y bueno. No inventes
   MEDIUMs para parecer util.
5. **Ningun hallazgo viaja sin su `fix`** — en todas las severidades, explicito y separable del
   diagnostico, copiable sin releerlo. Si no sabes como se arregla: `decision_externa`, o no es
   hallazgo. → §5
6. **Un hallazgo por causa raiz**, no uno por linea afectada.
   El `fix` de un bug incluye desde la primera pasada su criterio de cierre y el caso de test
   necesario segun la ley del repo: entrada, resultado esperado y como detecta el bug original.
   Tests faltantes del mismo arreglo complementan ese hallazgo, no una causa nueva. Conserva
   su identidad al actualizarlo y describe el riesgo residual si ya se corrigio el comportamiento.
7. **Nada generado ni vendorizado.** Si el bloque lo reescribe una herramienta (lockfiles,
   migraciones, marcadores `BEGIN:`/`END:`), borrarlo no es un fix. → §7
8. **No afirmes comportamiento de framework de memoria.** Cache, params y rendering cambian entre
   versiones mayores: lee la version instalada y citala en el `why`, o `confidence: baja`. → §8
9. **Completa la ventana y sigue el impacto demostrado.** Reutiliza consultas compartidas; busca
   callers/patrones cuando el cambio lo justifique. Incluye las ubicaciones de la ventana en el fix;
   fuera de ella, muestra para PR de seguimiento. No hagas censos del repo por cada hallazgo. → §9
10. **Si dos sitios definen lo mismo, el `fix` dice cual sobrevive y como el otro pasa a derivarse
   de el.** Nunca "sincronizarlos": eso solo aplaza la proxima divergencia. → §10
11. **El valor real de la config, no el fallback del codigo.** `process.env.X || "0"` no significa
   que `X` valga 0. Busca el valor efectivo en `.env*`, compose, `Dockerfile` o `buildspec*` y
   citalo; si vive en infra que no ves, `confidence: baja` y enuncia la condicion. → §11

## `decision_externa`

Marca el rol que decide cuando el arreglo **no esta al alcance de quien abre el PR**: infraestructura,
archivo marcado como no modificable, dependencia nueva, rollout por fases, o una respuesta de negocio.
La `severity` sigue siendo la real — describe el riesgo, no quien lo arregla — y el agregador los
lista aparte, fuera del conteo del veredicto. → `findings-rules.md`

## Verificacion de pendientes

Incluye siempre `verifications: []`. Para cada huella de pending/<reviewer>.json, emite exactamente
una entrada con `fingerprint`, `status` (`open`, `closed`, `discarded`), `evidence` y `reviewer`
(identico al dueño asignado). Describe la comprobacion concreta; archivo revisado o ausencia del
pattern en el diff no bastan para cerrar. Un pendiente open sin cambios no necesita repetir su
finding; si cambia severidad/fix/evidencia/decision externa, emite el finding actualizado.
Comprueba el criterio de cierre del fix previo, incluidos sus tests necesarios. Si queda solo
esa proteccion, informa open con evidencia y severidad residual; no cierres el bug y crees otro
missing-test por el mismo arreglo. Si el pendiente pertenece a otro revisor, referencia su huella
en el `why` para que el agregador coordine la fusion; no inventes verificaciones no asignadas.

`scope` y `sin_hallazgos_en` son obligatorios, incluso vacios. Hallazgos nuevos se escriben una
vez por huella; fuentes de varios revisores se curan despues. El agregador persiste decisiones
finales y descartes; el esquema de `curated.json` y CLI esta en `review-state.md`.
