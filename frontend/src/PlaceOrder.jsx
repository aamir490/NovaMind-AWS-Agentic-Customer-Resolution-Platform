import { useState } from 'react';
import { cartLineTotal, cartTotals, formatCartMoney, normalizeCartQuantity } from './cart.js';

const RETRYABLE_STATUSES = new Set([400, 403, 404, 409, 422]);

/** Cart review and simulated checkout. The request remains {items:[{sku,quantity}]}. */
export default function PlaceOrder({ api, items, onSuccess, onCancel, onUpdateQuantity,
  onRemove, onViewOrders }) {
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [result, setResult] = useState(null);
  const [submittedItems, setSubmittedItems] = useState([]);
  const [error, setError] = useState(null);
  const [uncertain, setUncertain] = useState(false);

  async function handleSubmit(event) {
    event.preventDefault();
    if (submitting || submitted || uncertain || items.length === 0) return;

    const snapshot = items.map((item) => ({ ...item }));
    setSubmitting(true);
    setSubmitted(true);
    setSubmittedItems(snapshot);
    setError(null);

    try {
      const order = await api.createMyOrder(snapshot.map(({ sku, quantity }) => ({ sku, quantity })));
      setResult(order);
      onSuccess?.(order);
    } catch (err) {
      const retryable = RETRYABLE_STATUSES.has(err?.status);
      if (retryable) {
        setError(err.message || 'Order could not be placed. Review the cart and try again.');
        setSubmitted(false);
      } else {
        setUncertain(true);
        setError('The checkout result could not be confirmed. Check My Orders before attempting another checkout.');
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (result) {
    return (
      <section className="place-order" aria-labelledby="order-success-heading">
        <div className="order-success" role="status" aria-live="polite">
          <span className="order-success-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
              strokeLinejoin="round" strokeLinecap="round">
              <path d="M22 11.08V12a10 10 0 11-5.93-9.14M22 4L12 14.01l-3-3" />
            </svg>
          </span>
          <h2 id="order-success-heading">Order placed</h2>
          <p>Your demo order has been received.</p>
          <dl className="case-facts order-summary-facts">
            <div><dt>Order ID</dt><dd className="order-id">{result.id}</dd></div>
            {result.items.map((item) => (
              <div key={item.sku}><dt>{item.name}</dt><dd>SKU: {item.sku} · Qty: {item.quantity}</dd></div>
            ))}
          </dl>
          <CartTotals items={submittedItems} label="Order total" />
          <div className="order-success-actions">
            <button type="button" onClick={onViewOrders}>View My Orders</button>
            <button type="button" className="button-secondary" onClick={onCancel}>Continue shopping</button>
          </div>
        </div>
      </section>
    );
  }

  if (items.length === 0) {
    return (
      <section className="place-order" aria-labelledby="empty-cart-heading">
        <div className="customer-empty cart-empty" role="status">
          <span className="customer-empty-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
              strokeLinejoin="round" strokeLinecap="round">
              <path d="M3 3h2l2.4 11.2a2 2 0 002 1.6h7.8a2 2 0 001.9-1.4L21 7H6M10 20h.01M18 20h.01" />
            </svg>
          </span>
          <h2 id="empty-cart-heading">Your cart is empty</h2>
          <p>Add in-stock products from the catalog to start a demo order.</p>
          <button type="button" onClick={onCancel}>Browse catalog</button>
        </div>
      </section>
    );
  }

  return (
    <section className="place-order" aria-labelledby="place-order-heading">
      <div className="order-form-header">
        <div><p className="eyebrow">Shopping cart</p><h2 id="place-order-heading">Review checkout</h2>
          <p className="catalog-intro">Confirm products and quantities before placing one demo order.</p></div>
        <button type="button" className="button-secondary" onClick={onCancel} disabled={submitting}>
          ← Continue shopping
        </button>
      </div>

      <form className="cart-checkout" onSubmit={handleSubmit} noValidate>
        <div className="cart-lines" aria-label="Cart items">
          {items.map((item) => {
            const lineTotal = cartLineTotal(item);
            return <article key={item.sku} className="cart-line">
              <img src={item.image_path} alt="" width="96" height="72" />
              <div className="cart-line-details">
                <span className="catalog-sku">{item.sku}</span>
                <h3>{item.name}</h3>
                <p>{formatCartMoney(item.unit_price)} each · {item.stock_quantity} in stock</p>
              </div>
              <label className="cart-line-quantity">
                <span>Quantity</span>
                <input type="number" min="1" max={Math.min(item.stock_quantity, 1000)} step="1"
                  value={item.quantity} disabled={submitting || submitted}
                  aria-label={`Quantity for ${item.name}`}
                  onChange={(event) => onUpdateQuantity?.(item.sku,
                    normalizeCartQuantity(item.stock_quantity, event.target.value))} />
              </label>
              <strong className="cart-line-total">{formatCartMoney(lineTotal)}</strong>
              <button type="button" className="cart-remove" disabled={submitting || submitted}
                onClick={() => onRemove?.(item.sku)} aria-label={`Remove ${item.name} from cart`}>
                Remove
              </button>
            </article>;
          })}
        </div>

        <aside className="cart-review case-panel" aria-label="Order summary">
          <div><p className="eyebrow">Order summary</p>
            <h3>{items.reduce((total, item) => total + item.quantity, 0)} items</h3></div>
          <CartTotals items={items} label="Order total" />
          <p className="order-ownership-note case-caption">
            Your account is identified automatically. This is a simulated checkout with no payment or shipping.
          </p>
          {error && <div className={uncertain ? 'checkout-uncertain' : 'customer-error'} role="alert">
            <strong>{uncertain ? 'Confirmation needed' : 'Checkout failed'}</strong><span>{error}</span>
          </div>}
          <button type="submit" disabled={submitting || submitted || uncertain} aria-busy={submitting}>
            {submitting ? 'Placing order…' : 'Place demo order'}
          </button>
          {uncertain && <button type="button" className="button-secondary" onClick={onViewOrders}>Check My Orders</button>}
          <span className="case-caption">One order request · no real transaction</span>
        </aside>
      </form>
    </section>
  );
}

function CartTotals({ items, label }) {
  const totals = cartTotals(items);
  return <dl className="cart-totals">
    {totals.map((total) => <div key={total.currency}><dt>{label}</dt>
      <dd>{formatCartMoney(total)}</dd></div>)}
  </dl>;
}
