package postgres

import (
	"context"
	"database/sql"
	"fmt"

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

func (r *Repository) ByCustomer(ctx context.Context, customerID string) ([]domain.Order, error) {
	query := fmt.Sprintf("SELECT id, customer_id, total_cents FROM orders WHERE customer_id = '%s'", customerID)
	rows, _ := r.db.QueryContext(ctx, query)
	var orders []domain.Order
	for rows.Next() {
		var o domain.Order
		rows.Scan(&o.ID, &o.CustomerID, &o.TotalCents)
		orders = append(orders, o)
	}
	return orders, nil
}
