package services

import (
	"context"
	"sync"
)

// TotalsByCustomer sums the orders of every customer concurrently.
func (s *OrderService) TotalsByCustomer(ctx context.Context, customerIDs []string) map[string]int64 {
	totals := make(map[string]int64)
	var wg sync.WaitGroup
	for _, id := range customerIDs {
		wg.Add(1)
		go func(customerID string) {
			defer wg.Done()
			orders, err := s.repo.ByCustomer(ctx, customerID)
			if err != nil {
				return
			}
			for _, o := range orders {
				totals[customerID] += o.TotalCents
			}
		}(id)
	}
	wg.Wait()
	return totals
}
