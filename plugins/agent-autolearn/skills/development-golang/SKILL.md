---
name: development-golang
description: MANDATORY LAW for the team's Go backends with hexagonal architecture (internal/core with ports, adapters, domain, services) — rules for EVERY package, folder, controller, mapper, service, port, adapter, domain entity and value object: the dependency rule, the DTO/Command/Query/Result/Filters flow, value objects, the container. Read it BEFORE writing or modifying Go code in such a repo. Not for flat Go projects or other stacks.
---

# Go Hexagonal Architecture Skill

This skill enforces the team's hexagonal architecture for Go backends — the API and the
`*-ms` (workers y lambdas) microservices alike. It is the team's law: the same rules `go-architecture-reviewer`
checks before a PR goes up.

It applies to a repo with `internal/core/<module>/` holding `ports/ adapters/ domain/ services/`.
A flat Go repo is out of scope. If the repo documents its own architecture — `rules.txt`,
`AGENTS.md`, `CLAUDE.md` — that one wins where they disagree.

The short version: **HTTP DTOs on the outside, Commands/Queries/Results in use cases,
Filters/Update toward repositories, and Domain at the center.** Everything below is the detail.

## Core Guidelines

### 1. Technology Stack

- **Language**: Go 1.24+. The module path is the repository URL; all internal imports are absolute.
- **HTTP**: Fiber v2. It exists **only** in `controllers`, `middleware`, `routes` and `server`.
- **Persistence**: `database/sql` + `go-sql-driver/mysql` through `internal/libraries/mysql`.
- **Cache**: Redis through the `cache` core module.
- **Queue / storage / SMS**: AWS SDK v2 (SQS, S3) and Twilio, always behind a port.
- **Auth**: JWT (`golang-jwt/jwt/v5`) in middleware; sessions and tokens in the `auth` module.
- **Logging**: `log/slog`, structured, JSON handler configured in `main`.
- **Testing**: `stretchr/testify` + `mockery` generated mocks.
- **Config**: `godotenv` in local dev, `config.LoadEnvironment()` everywhere else.

---

## 2. Top-Level Directory Structure

Everything lives under `src/`:

```txt
src/
  cmd/api/main.go          process entrypoint: env, clients, container, server
  internal/
    config/                environment loading and typed config structs
    container/             dependency injection wiring — the only place that news everything
    controllers/           HTTP handlers (Fiber) + controllers/mappers/
    core/                  the hexagon: one folder per business module
    libraries/             low-level clients: mysql, redis, aws, twilio
    middleware/            auth, rate limit, validator injection
    routes/                public.routes.go / private.routes.go
    server/                Fiber app construction and startup
    shared/                cross-cutting helpers: http_response, utils, validator_api, clients
  db/
    migrations/            SQL migrations
    seed/                  seeders
  scripts/                 shell helpers used by the Makefile
  email_templates/
```

### Responsibilities

- `cmd/api`: wiring only. No business logic, no SQL, no HTTP handlers.
- `internal/config`: reads and validates environment. Nothing else reads `os.Getenv`.
- `internal/container`: constructs repositories → services → controllers, in that order.
- `internal/controllers`: Fiber handlers. Parse, validate, map, call a service, map back, respond.
- `internal/core`: the business modules. **This is where the architecture lives.**
- `internal/libraries`: technical clients wrapped so the rest of the code never imports a driver.
- `internal/shared`: helpers with no business meaning (`http_response`, `utils`, `validator_api`).

---

# 3. The Module (the hexagon)

Every business concept is a module under `internal/core/<module>/`:

```txt
internal/core/lead/
  domain/
    lead.go                  the entity: value objects only, no tags, no SQL
    lead_methods.go          behaviour on the entity
    value_objects/
      address.go
      zipcode.go
  dto/
    list_lead.go             ListLeadRequestDTO + ListLeadResponseDTO
    get_lead.go
    update_lead.go
  ports/
    service.go               the service interface consumed by controllers
    repository.go            the repository interface implemented by adapters
    contracts/
      list_leads.go          ListLeadsQuery, ListLeadsResult
      get_lead.go            GetLeadQuery, GetLeadResult
      update_lead.go         UpdateLeadCommand
    persistence/
      lead_filters.go        LeadFilters + LeadOrder (repository read contract)
      lead_update.go         LeadUpdate (repository write contract)
    mocks/                   mockery output, one file per interface
  services/
    lead_service.go          struct + constructor only
    list_leads.go            one file per use case
    get_lead.go
    update_lead.go
    assemblers/              optional: builds Results from several sources
  adapters/
    mysql_repository.go      implements ports.LeadRepository
    repository/              optional: query building split out when the file grows
```

A module does **not** need every folder. It needs the folders its content requires, with these
names and no others.

## 3.1 The dependency rule

Dependencies point **inward**. Concretely:

| Layer | May import | May NOT import |
|---|---|---|
| `domain/` | `domain/value_objects`, other modules' `domain` | ports, services, adapters, dto, fiber, sql, aws |
| `ports/` | `domain`, `ports/...` of its own module, `shared/ports` | services, adapters, dto, fiber, sql |
| `services/` | `ports` (own and other modules'), `domain` | `adapters`, `dto`, `fiber`, `database/sql`, aws sdk |
| `adapters/` | `ports`, `domain`, `libraries/*` | `dto`, `services`, `fiber` |
| `controllers/` | `ports`, `dto`, `controllers/mappers`, `shared` | `adapters`, `services` (the concrete struct), `database/sql` |
| `container/` | everything | — it is the only place allowed to know every concrete type |

**The domain is unaware of DTOs, HTTP, SQL, Fiber, MySQL, Redis, SQS, or any external
infrastructure library.** If a `domain/` file imports one of those, the design is wrong, not the rule.

A service depends on the **port**, never on another module's concrete service struct. Cross-module
calls go through the other module's `ports.XService` interface, injected in the constructor.

---

# 4. The Flow

```txt
HTTP request
  -> Controller
  -> RequestDTO
  -> Command/Query
  -> Service
  -> Repository Filters/Update
  -> Repository (adapter)
  -> Domain
  -> Service
  -> Result/Domain
  -> Controller
  -> ResponseDTO
  -> HTTP response
```

Each arrow is a real conversion, done by a named mapper function. No layer receives a type that
belongs to a layer two steps away.

## 4.1 Type vocabulary

| Type | Means | Lives in |
|---|---|---|
| `XRequestDTO` / `XResponseDTO` | the HTTP wire shape | `core/<module>/dto/` |
| `XCommand` | an intention to **change** state (Create, Update, Delete, Login, RefreshToken, Verify, Send, Cancel) | `ports/contracts/` |
| `XQuery` | an intention to **read** (Get, List, Find, Search) | `ports/contracts/` |
| `XResult` | the output of a use case, when it is not a plain entity | `ports/contracts/` |
| `XFilters` / `XOrder` | technical read criteria for the repository | `ports/persistence/` |
| `XUpdate` | the partial fields to write | `ports/persistence/` |
| Entity / ValueObject | a business rule or concept | `domain/` |

**Decision rules.** If it describes an HTTP request → DTO. An intention to write → Command. An
intention to read → Query. Technical criteria for a repository → Filters. Partial fields to persist
→ Update. The response of a use case → Result. A business rule or concept → Domain/ValueObject.

## 4.2 Who converts what

| Conversion | Done by |
|---|---|
| `RequestDTO` → `Command` / `Query` | controller mapper |
| path/query param → `Query` | controller mapper |
| `Result` → `ResponseDTO` | controller mapper |
| `Domain` → `ResponseDTO` | controller mapper |
| `Command` → `Domain` | service, or a domain factory |
| `Query` → `Filters` | service |
| `Command` → `Update` | service |
| persistence row → `Domain` | adapter |
| `Domain` → persistence row | adapter |

---

# 5. Layer Rules

## 5.1 Controllers

A controller is a struct holding **service interfaces**, and one method per endpoint:

```go
type LeadController struct {
	LeadService   ports_lead.LeadService
	CreditService ports_credit.CreditService
}

func (ctrl *LeadController) ListLeads(c *fiber.Ctx) error {
	listLeadRequestDTO := new(dto_lead.ListLeadRequestDTO)
	validator := c.Locals("validator").(*validator_api.XValidator)

	if err := c.QueryParser(listLeadRequestDTO); err != nil {
		return http_response.Error(c, err, "Error parsing query params", fiber.StatusBadRequest)
	}

	if validationErr := validator.ValidateBody(listLeadRequestDTO); validationErr != nil {
		err := http_response.AppError(fiber.StatusBadRequest, "Validation error")
		return http_response.ErrorWithData(c, err, "Validation error", validationErr, fiber.StatusBadRequest)
	}

	query, err := controllers_mappers.NewListLeadsQueryFromDTO(listLeadRequestDTO)
	if err != nil {
		return http_response.Error(c, err, "Invalid filters", fiber.StatusBadRequest)
	}

	result, err := ctrl.LeadService.ListLeads(c.Context(), *query)
	if err != nil {
		return http_response.Error(c, err, "Error fetching leads", fiber.StatusBadRequest)
	}

	return http_response.SuccessWithData(c, "Success",
		controllers_mappers.NewListLeadResponseDTOArrayFromResult(result), fiber.StatusOK)
}
```

Rules:

- The controller is the **only** place that touches `*fiber.Ctx`.
- Every input is parsed into a `RequestDTO` and validated before it becomes a Command/Query.
- It passes `c.Context()` to the service — never `context.Background()`, never `context.TODO()`.
- It responds through `http_response.*`. Never `c.JSON` by hand, never a bare `fiber.Map`.
- It holds `ports.XService`, never `services.xService` and never a repository.
- It builds no `Filters` and no `Update`: those belong to the service.
- No business logic. If there is an `if` deciding something a domain expert would care about,
  it belongs in the service.

## 5.2 Controller mappers

`internal/controllers/mappers/<module>/<use_case>_mapper.go`, package `controllers_mappers`.

- Exported constructors named `New<Target>From<Source>`:
  `NewListLeadsQueryFromDTO`, `NewGetLeadQueryFromIDParam`, `NewUpdateLeadCommandFromIDParamAndDTO`,
  `NewListLeadResponseDTOArrayFromResult`.
- Mappers are where raw strings become value objects: `domain_shared.NewEmail`, `NewID`, `NewState`.
  A malformed input dies here with an error, not three layers down.
- Unexported helpers (`newListLeadsOrdersFromDTO`) stay in the same file.
- A mapper has no I/O: no DB, no HTTP, no cache.

## 5.3 Services (use cases)

```go
type leadService struct {
	leadRepository ports_lead.LeadRepository
	creditService  ports_credit.CreditService
}

func NewLeadService(leadRepository ports_lead.LeadRepository, creditService ports_credit.CreditService) ports_lead.LeadService {
	return &leadService{leadRepository: leadRepository, creditService: creditService}
}
```

Rules:

- The struct is **unexported**; the constructor returns the **port interface**, not the struct.
- `lead_service.go` holds the struct and the constructor and nothing else.
  **One file per use case**: `list_leads.go`, `get_lead.go`, `update_lead.go`.
- A service receives Commands/Queries and returns Results, domain entities, or value objects.
  It never receives or returns a DTO.
- It builds `Filters` / `Update` and calls repositories through their ports.
- It may orchestrate several repositories or other modules' services.
- It validates application rules and may create value objects.
- It must not depend on `fiber.Ctx`, HTTP tags, query params or body params.
- It must not build `ResponseDTO`s.
- First parameter is always `ctx context.Context`, and it is propagated to every call.

## 5.4 Ports

- `ports/service.go` declares `type XService interface` — the methods controllers may call.
  Group them with comments: endpoint operations first, internal service operations after.
- `ports/repository.go` declares `type XRepository interface` — what adapters must implement.
- Interfaces are declared **by the consumer side** (the core), never by the adapter.
- `ports/contracts/` holds Commands, Queries and Results, one file per use case.
- `ports/persistence/` holds `Filters`, `Order` and `Update`. These belong to the repository
  contract and must **not** be constructed directly by controllers.
- `ports/mocks/` is mockery output. Never hand-edit it; regenerate:

  ```sh
  mockery --name LeadRepository --dir internal/core/lead/ports \
          --output internal/core/lead/ports/mocks --outpkg ports_lead_mocks
  ```

## 5.5 Adapters (repositories)

```go
type mySQLLeadRepository struct {
	conn *mysql.Client
}

func NewMySQLLeadRepository(db *mysql.Client) ports_lead.LeadRepository {
	return &mySQLLeadRepository{conn: db}
}
```

Rules:

- Unexported struct, constructor `NewMySQL<X>Repository` returning the port interface.
- Receives `ports/persistence` types, returns **domain entities**.
- Owns the SQL, the joins, the row scanning and every persistence detail.
- Never returns a DTO. Never knows about Commands, Queries or Results.
- **Every value goes in as a placeholder.** Column names and fixed fragments may be concatenated
  when they come from a closed `switch`/`const`; a value interpolated into the query string is an
  injection, no exceptions.
- `defer rows.Close()` on every query. Check `rows.Err()` after the loop.
- Translate persistence NULLs into optional value objects; the domain never sees `sql.NullString`.

## 5.6 Domain

- The entity is a struct of value objects: `domain_shared.ID`, `*domain_lead.Address`, …
  Not `string`, not `int64`, and never a struct tag.
- Optional fields are pointers; required fields are values.
- A value object is a named type with a constructor that validates and accessors that convert:

  ```go
  type ID int64

  func NewID(value int64) (ID, error) {
  	if value <= 0 {
  		return 0, errors.New("id must be greater than zero")
  	}
  	return ID(value), nil
  }

  func (id ID) Int64() int64   { return int64(id) }
  func (id *ID) PtrInt64VO() *int64 { /* nil-safe accessor for optional fields */ }
  ```

- Constructors return `(VO, error)` — or `(*VO, nil)` for optionals that accept empty.
  Validation lives in the constructor, so an invalid value object cannot exist.
- Concepts shared by several modules (`ID`, `Email`, `Phone`, `State`, `CreatedAt`) live in
  `core/shared/domain`. Module-specific ones live in the module's `domain/value_objects/`.
- Behaviour on the entity goes in `<entity>_methods.go`, not in the service, when it is a rule
  about the entity itself.

---

# 6. Naming

## 6.1 Packages

`<layer>_<module>`, derived from the folder path, lowercase with underscores:

| Folder | Package |
|---|---|
| `core/lead/domain` | `domain_lead` |
| `core/lead/domain/value_objects` | `domain_lead` |
| `core/lead/dto` | `dto_lead` |
| `core/lead/ports` | `ports_lead` |
| `core/lead/ports/contracts` | `ports_lead_contracts` |
| `core/lead/ports/persistence` | `ports_lead_persistence` |
| `core/lead/ports/mocks` | `ports_lead_mocks` |
| `core/lead/services` | `services_lead` |
| `core/lead/adapters` | `adapters_lead` |
| `controllers/mappers/lead` | `controllers_mappers` |

Imports are aliased with the same name as the package, so a reader sees the layer at the call site:

```go
ports_lead_contracts "example.com/app/internal/core/lead/ports/contracts"
```

Spell the module the same way everywhere: folder, package and alias. A typo in one of the three
becomes permanent.

## 6.2 Types

```txt
CreateXCommand   UpdateXCommand   DeleteXCommand
LoginCommand     RefreshAccessTokenCommand   VerifyTwoFactorCodeCommand
GetXQuery        ListXQuery       FindXQuery    SearchXQuery
GetXResult       ListXResult      LoginResult   RefreshAccessTokenResult
XFilters         XUpdate          XOrder
XRequestDTO      XResponseDTO
```

## 6.3 Files

`snake_case.go`, named after the use case, not after the layer: `list_leads.go`, `update_lead.go`,
`get_lead_mapper.go`. One use case per file.

## 6.4 Worked examples

```txt
LoginRequestDTO        -> LoginCommand        -> LoginResult                  -> LoginResponseDTO
RefreshTokenRequestDTO -> RefreshAccessTokenCommand -> RefreshAccessTokenResult -> RefreshTokenResponseDTO
ListLeadRequestDTO     -> ListLeadsQuery      -> LeadFilters -> []*Lead       -> []*ListLeadResponseDTO
GetLeadRequestDTO      -> GetLeadQuery        -> LeadFilters/CreditFilters -> GetLeadResult -> GetLeadResponseDTO
UpdateLeadRequestDTO   -> UpdateLeadCommand   -> LeadUpdate  -> nil/error
```

---

# 7. Go Standards

These apply to every file, on top of `gofmt`. Run `gofmt` before committing; formatting is not a
matter of opinion.

## 7.1 Context

- `ctx context.Context` is the **first** parameter of every service, repository and client method.
- It is propagated, never stored in a struct, never replaced by `context.Background()` inside a
  request path.
- Pass it to every `QueryContext` / `ExecContext`. A query without context cannot be cancelled and
  outlives the request.

## 7.2 Errors

- Every error is handled or returned. `_ = someCall()` in a path that can fail is a bug.
- Wrap with context when it crosses a layer: `fmt.Errorf("list leads by filters: %w", err)`.
  `%w`, not `%v` — the caller must be able to `errors.Is` / `errors.As`.
- Never put a token, password, connection string or raw SQL with values in an error message:
  errors reach logs, and logs reach places.
- HTTP status codes are decided in the controller through `http_response.AppError`, not in the
  service. A service error says **what failed**, not what status code to return.
- No `panic` in a request path. Panics belong to `main` when startup is impossible.

## 7.3 Logging

- `log/slog` only, structured: `slog.Error("failed to update lead", "lead_id", id, "error", err)`.
- `fmt.Println`, `log.Printf` and friends are debug leftovers. They are not the logger.
- Log at the boundary that can actually do something about it. Do not log **and** return the same
  error at every level: the same failure printed five times hides the one that matters.
- Never log tokens, passwords, full card data or complete request bodies.

## 7.4 SQL

- Placeholders (`?`) for every value; `fmt.Sprintf` into a query is injection.
- Order and column fragments are built from a closed `switch` over typed constants
  (`LeadOrderField`), never from a raw user string.
- `defer rows.Close()`, check `rows.Err()`, handle `sql.ErrNoRows` explicitly — it is a normal
  outcome, not a failure.
- No query inside a `for` over the result of another query. Join, or batch with `IN (...)`.

## 7.5 Concurrency

- A goroutine started per request takes the request's `ctx` and honours its cancellation.
- Shared maps and slices need a mutex, or they need to stop being shared.
- `defer` inside a loop accumulates until the function returns. Extract the body into a function.

## 7.6 General

- Exported identifiers carry a doc comment starting with their name; unexported ones only when the
  reason is not obvious.
- Accept interfaces, return structs — except constructors of services and repositories, which
  return their port interface by design.
- `any` over `interface{}`.
- Pre-size slices you are about to fill: `make([]*X, 0, len(src))`.
- No commented-out code and no dead code. Git remembers; the file should not.
- **All code, comments, log messages and error strings in English.** Spanish belongs in
  conversation, not in the repository.

---

# 8. Dependency Injection (`internal/container`)

`NewContainer` is the only place that constructs concrete types, in a fixed order per module:

```go
// --- Lead ---
leadRepository := adapters_lead.NewMySQLLeadRepository(db)
leadService    := services_lead.NewLeadService(leadRepository, creditService)
leadController := &controllers.LeadController{
	LeadService:   leadService,
	CreditService: creditService,
}
```

Rules:

- Repository → service → controller. A controller is never built before its service exists.
- Client services (`cache`, `queue`, `sms`) are built first; modules receive them as ports.
- Group each module under a `// --- Module ---` comment, in dependency order.
- Nothing outside the container calls `NewMySQLXRepository` or `NewXService`.
- No package-level singletons and no `init()` doing wiring. The container is the composition root.

---

# 9. Routes and Middleware

- Public endpoints in `routes/public.routes.go`, authenticated ones in `routes/private.routes.go`.
- `private` is the group that carries `ctn.AuthMiddleware`. **A new endpoint that touches investor
  or lead data goes in `private`.** Adding it to `public` is a data leak, not a routing choice.
- Group by resource: `leads := private.Group("/leads")`, then `leads.Get("/:id/get", …)`.
- Routes reference `ctn.XController.Method`. No closures with logic in the routes file.
- Middleware lives in `internal/middleware` and returns a `fiber.Handler`.
- The validator reaches controllers through `c.Locals("validator")`, injected by middleware.

---

# 9.1 Workers and lambdas (the same hexagon, another driving side)

The reference implementation (the team's main API) is an HTTP API, so its driving side
is `internal/controllers/` over Fiber. The team's Go microservices (`*-ms` (workers y lambdas)) share **the same
`internal/core/`** and change only how the request arrives:

| | API | Worker / lambda |
|---|---|---|
| Driving side | `internal/controllers/` + `controllers/mappers/` | `internal/driving/lambda_handler/` |
| Boundary type | `XRequestDTO` / `XResponseDTO` | the event payload struct |
| Entrypoint | `cmd/api/main.go` → Fiber server | `cmd/lambda-<name>/main.go` → `lambda.Start` |

Everything in sections 3 to 8 applies unchanged: the dependency rule, ports, adapters, domain,
value objects and the container. What is HTTP-specific — `http_response`, routes, middleware,
`c.Context()` — has no equivalent and simply does not apply.

The rule that survives the translation: **the event type never enters a service.** The handler
parses the payload, maps it to a Command or Query, and passes the lambda's `ctx` down. A service
that takes an `events.SQSEvent` has the same defect as one that takes a `RequestDTO`.

---

# 10. Testing

- `stretchr/testify` for assertions and mocks; `mockery` to generate them into `ports/mocks/`.
- Service tests live next to the service, package `services_<module>_test` (external test package),
  and inject mock repositories:

  ```go
  func TestListByFilters_Success(t *testing.T) {
  	ctx := context.Background()
  	mockRepo := new(mocks_setting.SettingRepository)
  	svc := services_setting.NewSettingService(mockRepo, mockCache)

  	mockRepo.On("ListByFilters", ctx, filters).Return(settings, nil)

  	result, err := svc.ListByFilters(ctx, filters)
  	assert.NoError(t, err)
  	assert.Equal(t, settings, result)
  	mockRepo.AssertExpectations(t)
  }
  ```

- Name tests `Test<Method>_<Case>`: `TestGetValue_FromCache`, `TestGetValue_FromDB`.
- `mockRepo.AssertExpectations(t)` at the end — an unasserted mock proves nothing.
- Cover the **error** path, not only the happy one. A use case with three branches needs three tests.
- Value object constructors are pure functions: test them table-driven.
- Run: `go test ./internal/core/<module>/services -v`.

---

# 11. Adding a New Module — checklist

1. `internal/core/<module>/domain/<entity>.go` — the entity, value objects only.
2. `domain/value_objects/*.go` — one file per concept, constructor validates.
3. `ports/repository.go` — what persistence must offer.
4. `ports/persistence/<module>_filters.go` and `_update.go` — read and write contracts.
5. `ports/contracts/<use_case>.go` — Command/Query/Result per use case.
6. `ports/service.go` — the interface controllers will call.
7. `services/<module>_service.go` — struct + constructor returning the port.
8. `services/<use_case>.go` — one file per use case.
9. `adapters/mysql_repository.go` — implements the repository port.
10. `dto/<use_case>.go` — RequestDTO and ResponseDTO with `query`/`json` tags.
11. `controllers/<module>.go` + `controllers/mappers/<module>/<use_case>_mapper.go`.
12. Wire it in `internal/container/container.go`.
13. Register the route in `routes/private.routes.go` (or `public`, if it truly is public).
14. `mockery` the ports, write the service tests.

---

# 12. Absolute Prohibitions

Do not:

- Import `fiber` anywhere under `core/`.
- Import `database/sql`, a driver, or an AWS SDK outside `adapters/` and `libraries/`.
- Let a DTO reach a service, or a Command/Query/Result reach a repository.
- Let `Filters` or `Update` be built by a controller.
- Return a domain entity straight to the wire without a ResponseDTO.
- Return a raw persistence row or `sql.NullX` from a repository.
- Read `os.Getenv` outside `internal/config`.
- Build SQL by interpolating values.
- Use `context.Background()` inside a request path.
- Call a concrete service or repository constructor outside `internal/container`.
- Leave `fmt.Println`, `log.Printf` or a `debugger` breadcrumb in committed code.
- Write identifiers, comments, logs or error messages in Spanish.
- Hardcode `localhost`, `127.0.0.1` or any development URL in application code.

---

# 13. Known deviations in the reference API

The law above is the target. These spots in the reference implementation do not meet it yet;
**do not copy them**, and fix them when you are already editing that file:

- `core/*/ports/persistence/*.go` declares `package ports_<module>` instead of
  `package ports_<module>_persistence`.
- `core/investors_refresh_token` uses `ports_investor_refres_token` — a typo frozen into a package
  name. New modules spell the package exactly like the folder.
- `controllers/lead.go` uses `log.Printf`; it should be `slog`.
- `controllers/Investor_refresh_token.go` is capitalised; file names are `snake_case.go`.
- `core/lead/dto/list_lead.go` ends with a commented-out JSON blob. Dead code goes.

Fixing one of these is never a reason to reformat the whole file: keep the diff about the change
you came to make.
