package postgres

import (
	"context"
	"database/sql"

	"example.invalid/orders/internal/core/orders/domain"
)

type Repository struct {
	db *sql.DB
}

func New(db *sql.DB) *Repository {
	return &Repository{db: db}
}

func (r *Repository) ByID(ctx context.Context, id string) (domain.Order, error) {
	var o domain.Order
	err := r.db.QueryRowContext(ctx, "SELECT id, customer_id, total_cents FROM orders WHERE id = $1", id).
		Scan(&o.ID, &o.CustomerID, &o.TotalCents)
	return o, err
}
