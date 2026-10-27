import { useEffect, useRef, useState } from 'react';

/**
 * My Orders — lists all orders belonging to the authenticated customer,
 * with inline order detail on selection.
 *
 * Props
 * -----
 * api             — createApi() instance
 * onReportProblem — called with the selected Order when customer clicks
 *                   "Report a problem" from the order detail view
 */
export default function MyOrders({ api, onReportProblem }) {
  const [orders, setOrders]   = useState(null);   // null = not yet loaded
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState(null);
  const [selected, setSelected] = useState(null); // order object for detail view
  const requestRef = useRef(0);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  function load() {
    const version = ++requestRef.current;
    setLoading(true);
    setError(null);
    setSelected(null);
    api.listMyOrders()
      .then((data) => {
        if (requestRef.current !== version) return;
        // Sort newest-looking IDs last (UUIDs are time-neutral, so we keep
        // insertion order from the server and just reverse for recency).
        setOrders([...data].reverse());
      })
      .catch((err) => {
        if (requestRef.current !== version) return;
        setError(err.message || 'Could not load orders. Check the API and try again.');
      })
      .finally(() => { if (requestRef.current === version) setLoading(false); });
  }

  // -----------------------------------------------------------------
  // Order detail pane
  // -----------------------------------------------------------------
  if (selected) {
    return (
      <section className="my-orders" aria-labelledby="order-detail-heading">
        <div className="orders-header">
          <div>
            <p className="eyebrow">Order detail</p>
            <h2 id="order-detail-heading">Order summary</h2>
          </div>
          <button type="button" className="button-secondary" onClick={() => setSelected(null)}>
            ← Back to my orders
          </button>
        </div>

        <div className="order-detail case-panel">
          <dl className="case-facts">
            <div>
              <dt>Order ID</dt>
              <dd className="order-id">{selected.id}</dd>
            </div>
          </dl>

          <h3 className="order-items-heading">Items in this order</h3>
          <ul className="case-item-list" aria-label="Ordered items">
            {selected.items.map((item, index) => (
              <li key={`${item.sku}-${index}`}>
                <div>
                  <strong>{item.name}</strong>
                  <span className="case-item-sku">{item.sku}</span>
                </div>
                <span className="case-item-quantity">Qty {item.quantity}</span>
              </li>
            ))}
          </ul>

          <p className="case-caption order-detail-note">
            This order was placed through the NovaMind demo portal.
            No payment or shipment has been processed.
          </p>
          {onReportProblem && (
            <div className="order-detail-intake">
              <button
                type="button"
                onClick={() => onReportProblem(selected)}
              >
                Report a problem
              </button>
              <span className="case-caption">Have an issue with this order? Our team will review it.</span>
            </div>
          )}
        </div>
      </section>
    );
  }

  // -----------------------------------------------------------------
  // Order list
  // -----------------------------------------------------------------
  return (
    <section className="my-orders" aria-labelledby="my-orders-heading">
      <div className="orders-header">
        <div>
          <p className="eyebrow">My orders</p>
          <h2 id="my-orders-heading">Your orders</h2>
        </div>
        <button
          type="button"
          className="button-secondary"
          onClick={load}
          disabled={loading}
          aria-busy={loading}
        >
          {loading ? 'Loading…' : 'Refresh orders'}
        </button>
      </div>

      {loading && (
        <p className="customer-notice" role="status" aria-live="polite">
          Loading your orders…
        </p>
      )}

      {error && (
        <p className="customer-error" role="alert">{error}</p>
      )}

      {!loading && !error && orders === null && (
        <div className="customer-empty" role="status">
          <span className="customer-empty-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
              strokeLinejoin="round" strokeLinecap="round">
              <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
            </svg>
          </span>
          <h3>No orders loaded</h3>
          <p>Click "Refresh orders" to load your order history.</p>
        </div>
      )}

      {!loading && orders !== null && orders.length === 0 && (
        <div className="customer-empty" role="status">
          <span className="customer-empty-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
              strokeLinejoin="round" strokeLinecap="round">
              <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
            </svg>
          </span>
          <h3>No orders yet</h3>
          <p>You haven't placed any orders yet. Browse the catalog to get started.</p>
        </div>
      )}

      {orders !== null && orders.length > 0 && (
        <ul className="orders-list" aria-label="Your orders">
          {orders.map((order) => (
            <li key={order.id} className="order-row case-panel">
              <div className="order-row-body">
                <p className="order-id">{order.id}</p>
                <ul className="order-row-items" aria-label="Items">
                  {order.items.map((item, i) => (
                    <li key={`${item.sku}-${i}`} className="order-row-item">
                      <strong>{item.name}</strong>
                      <span className="case-item-sku">{item.sku}</span>
                      <span className="case-item-quantity">Qty {item.quantity}</span>
                    </li>
                  ))}
                </ul>
              </div>
              <button
                type="button"
                className="button-secondary order-detail-btn"
                onClick={() => setSelected(order)}
                aria-label={`View details for order ${order.id}`}
              >
                View details
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
