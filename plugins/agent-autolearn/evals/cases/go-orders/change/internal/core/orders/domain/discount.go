package domain

import (
	"context"

	"example.invalid/orders/internal/core/orders/adapters/loyalty"
)

// ApplyLoyaltyDiscount halves the total for members of the loyalty programme.
func ApplyLoyaltyDiscount(ctx context.Context, client *loyalty.Client, o Order) (Order, error) {
	member, err := client.IsMember(ctx, o.CustomerID)
	if err != nil {
		return o, err
	}
	if member {
		o.TotalCents = o.TotalCents / 2
	}
	return o, nil
}
