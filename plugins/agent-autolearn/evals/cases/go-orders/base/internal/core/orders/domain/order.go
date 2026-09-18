package domain

import "errors"

var ErrInvalidOrder = errors.New("invalid order")

type Order struct {
	ID         string
	CustomerID string
	TotalCents int64
}

func NewOrder(id, customerID string, totalCents int64) (Order, error) {
	if id == "" || customerID == "" || totalCents < 0 {
		return Order{}, ErrInvalidOrder
	}
	return Order{ID: id, CustomerID: customerID, TotalCents: totalCents}, nil
}
