package ports

import (
	"context"

	"example.invalid/orders/internal/core/orders/domain"
)

type OrderRepository interface {
	ByID(ctx context.Context, id string) (domain.Order, error)
	ByCustomer(ctx context.Context, customerID string) ([]domain.Order, error)
}
