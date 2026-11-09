---
name: readiness-file
description: Crea o actualiza el .deploy-readiness.json del repo, el archivo donde el chequeo deploy readiness del worker de PRs lee de que Lambdas, secretos, servicios ECS, reglas, schedules y schema MySQL saca sus variables cada entorno. Lee los archivos de despliegue del repo y, si hay AWS CLI con credenciales, busca los recursos reales por nombre (solo lectura, nunca valores). Usar al pedir crear, generar, rellenar o arreglar el .deploy-readiness.json, cuando un PR quedo bloqueado por "No usable .deploy-readiness.json" o por una plantilla con <FILL_ME>, o invocar /readiness-file.
---

# Readiness File

Generas el `.deploy-readiness.json` de un repo: por cada entorno, los recursos de AWS donde el codigo
desplegado lee sus variables de entorno, y el schema MySQL que usa. El worker de PRs lo lee desde la
rama destino del PR para verificar que las variables nuevas existen antes de mergear.

**Comandos con valores literales.** `$ROOT`, `$PROFILE` y las demas variables de los ejemplos son
marcadores: escribe su valor real en cada comando. No ejecutes comandos con `$VAR`, `${VAR}`, `$(...)`
ni backticks.

## Reglas que no se rompen

- **AWS solo en lectura y solo nombres.** Usa unicamente los comandos `list-*`/`describe-*` de esta
  skill. **Nunca** `secretsmanager get-secret-value`, `ssm get-parameter --with-decryption`, ni
  `lambda get-function-configuration` sin `--query` que deje solo las claves: no leas ni muestres un
  valor. No crees, modifiques ni borres nada en AWS.
- **No inventes nombres.** Un recurso entra al archivo solo si lo viste en un listado de AWS o
  escrito literal en un archivo del repo. Lo que no puedas confirmar va como `<FILL_ME ...>` y lo dices.
- **El archivo es del repo.** Lo escribes en la raiz y lo muestras; no haces commit ni push salvo que
  el usuario lo pida.

## 1. Formato que tienes que producir

```json
{
  "dev": {
    "lambda": ["leads-api-DEV"],
    "secret": ["leads/dev"],
    "mysql_schema": "msc_test"
  },
  "prod": {
    "ecs_service": ["SELIGERIDEAS-VIRGINIA-CLUSTER/seligerideas-virginia-msc-leads-verify-api-PROD-service"],
    "event_rule": ["seligerideas-virginia-LEADS-SCRIPT-PROD-st"],
    "mysql_schema": {"secret": "leads/prod", "key": "DB_DATABASE"}
  }
}
```

| Clave | Valor | Que es | Cuando |
|---|---|---|---|
| `lambda` | lista | Nombre o ARN de la funcion | Variables puestas directo en la Lambda |
| `secret` | lista | Nombre o ARN del secreto (objeto JSON plano) | El codigo o su `.env` las carga de Secrets Manager |
| `ecs_service` | lista | `cluster/service` | Corre como servicio ECS; ya cubre su `environment` y sus `secrets` |
| `event_rule` | lista | `regla`, o `bus/regla` fuera del bus default | Corre **solo** como tarea programada de EventBridge |
| `schedule` | lista | `schedule`, o `grupo/schedule` | Igual, con EventBridge Scheduler |
| `mysql_schema` | un nombre, o `{"secret", "key"}` | El schema que usa | Si el repo usa MySQL |

- Las claves de primer nivel son los entornos del worker (`READINESS_ENVIRONMENTS`; por defecto `dev`
  para `development` y `prod` para `main`).
- Omite los tipos que el repo no usa. Una variable cuenta como presente si **cualquier** fuente la
  tiene, asi que listar de mas no bloquea: lista lo que el codigo desplegado lee de verdad.
- Un entorno sin fuentes se declara vacio: `"prod": {}`.
- Claves `$comment` en el primer nivel se ignoran: si el archivo viene de la plantilla del worker,
  borrala al terminar.

## 2. Situacion de partida

`$ROOT` es la raiz del repo (`git rev-parse --show-toplevel`).

1. Si `$ROOT/.deploy-readiness.json` existe, leelo. Con valores `<FILL_ME ...>` es la plantilla que
   subio el worker: tu trabajo es rellenarla. Sin ellos, es una actualizacion: conserva lo que ya
   esta salvo que encuentres que sobra o falta algo, y dilo.
2. Entornos: si el usuario no los nombro, usa `dev` y `prod` y dilo en una linea. Si el repo solo
   despliega a uno, igual declara los dos (el otro vacio solo si de verdad no tiene fuentes, y
   pregunta antes).

## 3. Leer el repo

Busca como se despliega y de donde salen sus variables. Mira, segun existan:

- `template.yaml`/`template.yml`, `samconfig.toml`, `serverless.yml`: nombres de funciones,
  `Environment.Variables`, referencias `{{resolve:secretsmanager:...}}`, parametros por entorno.
- `buildspec*.yml`, `Dockerfile*`, `docker-compose*.yml`, scripts de `deploy/`, `infra/`, `.github/`,
  `bitbucket-pipelines.yml`: comandos que arman un `.env` desde un secreto (`get-secret-value
  --secret-id X`), nombres de cluster/servicio, task definitions (`*task-definition*.json`).
- `.env.example` y la configuracion de la app (`config/`, `application/config/database.php`,
  `src/internal/config/`): que variables lee y como se llama la del nombre de la base
  (`DB_DATABASE`, `DB_NAME`, `MYSQL_DATABASE`...).

Anota cada pista con su archivo y linea. Es la evidencia que mostraras al final.

## 4. Buscar en AWS (si se puede)

**Disponibilidad.** `command -v aws`. Si no esta, salta a la seccion 5 y dilo.

**Perfil.** Lista los perfiles: `aws configure list-profiles`. Con uno solo (o solo `default`),
usalo. Con varios, pregunta con **AskUserQuestion** «¿Con que perfil de AWS busco los recursos?»
(hasta 4 opciones, el resto en «Other»). Comprueba que responde, sin mostrar mas que la cuenta:

```bash
aws --profile PROFILE sts get-caller-identity --query Account --output text
```

Si falla (sesion SSO vencida: sugiere `aws sso login --profile PROFILE`; sin credenciales), sigue sin
AWS. La region es la del perfil; si no tiene, pregunta o usa la que aparezca en los archivos del repo.

**Listados (solo nombres).** Ejecuta los que apliquen segun lo que encontraste en el repo:

```bash
aws --profile PROFILE lambda list-functions --query "Functions[].FunctionName" --output text
aws --profile PROFILE secretsmanager list-secrets --query "SecretList[].Name" --output text
aws --profile PROFILE ecs list-clusters --query "clusterArns" --output text
aws --profile PROFILE ecs list-services --cluster CLUSTER --query "serviceArns" --output text
aws --profile PROFILE events list-rules --query "Rules[?ManagedBy==null].Name" --output text
aws --profile PROFILE scheduler list-schedules --query "Schedules[].[GroupName,Name]" --output text
```

Las cuentas pueden tener cientos de recursos: filtra por el nombre del repo, sus siglas y las pistas
del paso 3 antes de mostrar nada. Para saber que corre una regla o un schedule, mira solo su destino:

```bash
aws --profile PROFILE events list-targets-by-rule --rule RULE --query "Targets[].EcsParameters.TaskDefinitionArn" --output text
```

Si necesitas confirmar que una Lambda tiene sus variables directas, mira **solo las claves**:

```bash
aws --profile PROFILE lambda get-function-configuration --function-name FUNCTION --query "keys(Environment.Variables)" --output text
```

**Entornos en una misma cuenta.** DEV y PROD pueden compartir cuenta. Separa por el sufijo o prefijo
del nombre (`-DEV`, `-PROD`, `_dev`, `prod/`...) y por las pistas del repo. Si un recurso no dice a
que entorno pertenece, no lo asignes: preguntalo.

**El schema.** Si el codigo lee el nombre de la base de una variable que esta en un secreto del
entorno (por ejemplo `DB_DATABASE` en `leads/prod`), usa `{"secret": "leads/prod", "key":
"DB_DATABASE"}`: el verifier lo lee en AWS sin que el nombre salga. Si el nombre aparece literal en el
repo para ese entorno, usalo directo. No abras el secreto para averiguarlo.

## 5. Sin AWS

Arma el archivo solo con lo que el repo dice literalmente. Cada nombre que no puedas confirmar va como
`<FILL_ME tipo de recurso>` (el worker lo rechaza hasta que alguien lo reemplace, asi que nunca queda
aprobado por error). Di que nombres hay que completar y donde buscarlos en la consola de AWS.

## 6. Confirmar y escribir

Muestra la propuesta por entorno, cada fuente con su evidencia (archivo:linea, o «listado AWS:
coincide con el nombre del repo y el sufijo -PROD»). Pregunta con **AskUserQuestion**:
«¿Escribo este .deploy-readiness.json?» con «Escribirlo (Recomendado)», «Quitar o cambiar
fuentes» y «Cancelar». Si pide cambios, aplicalos y vuelve a preguntar.

Escribe `$ROOT/.deploy-readiness.json` con 2 espacios de indentacion y valida que sea JSON:

```bash
python3 -m json.tool ROOT/.deploy-readiness.json
```

Comprueba ademas, a mano, lo que el worker rechaza: claves desconocidas, `ecs_service` sin `/`,
`mysql_schema` como lista, nombres con espacios, y ningun `<FILL_ME` si dijiste que esta completo.

## 7. Cerrar

Resume en pocas lineas: fuentes por entorno, que quedo como `<FILL_ME>` y por que, y el siguiente
paso. El worker lee el archivo **de la rama destino del PR**: tiene que llegar a `development` y a
`main` (o via el PR que lo agrega, que se verifica con su propio archivo sin bloquear). Ofrece hacer
commit solo si el usuario lo pide.
