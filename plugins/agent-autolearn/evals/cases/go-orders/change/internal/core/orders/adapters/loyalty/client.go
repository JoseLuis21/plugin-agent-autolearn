package loyalty

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	baseURL string
	http    *http.Client
}

func New(baseURL string) *Client {
	return &Client{baseURL: baseURL, http: &http.Client{Timeout: 3 * time.Second}}
}

// IsMember reports whether the customer belongs to the loyalty programme.
func (c *Client) IsMember(ctx context.Context, customerID string) (bool, error) {
	endpoint := c.baseURL + "/members/" + url.PathEscape(customerID)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return false, err
	}
	response, err := c.http.Do(request)
	if err != nil {
		return false, err
	}
	defer response.Body.Close()
	switch response.StatusCode {
	case http.StatusOK:
		return true, nil
	case http.StatusNotFound:
		return false, nil
	default:
		return false, fmt.Errorf("loyalty: unexpected status %d", response.StatusCode)
	}
}
