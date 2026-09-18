package services

import (
	"context"

	"example.invalid/orders/internal/core/orders/domain"
	"example.invalid/orders/internal/core/orders/ports"
)

type OrderService struct {
	repo ports.OrderRepository
}

func NewOrderService(repo ports.OrderRepository) *OrderService {
	return &OrderService{repo: repo}
}

func (s *OrderService) Get(ctx context.Context, id string) (domain.Order, error) {
	return s.repo.ByID(ctx, id)
}
