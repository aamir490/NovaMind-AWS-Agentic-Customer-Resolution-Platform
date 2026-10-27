import { useState } from 'react';
import CustomerCatalog from './CustomerCatalog.jsx';
import PlaceOrder from './PlaceOrder.jsx';
import MyOrders from './MyOrders.jsx';
import ReportProblem from './ReportProblem.jsx';
import MySupportRequests from './MySupportRequests.jsx';
import { addProductToCart, removeCartItem, updateCartQuantity } from './cart.js';

/**
 * Customer Portal — top-level container for the customer-facing experience.
 *
 * Manages customer sub-views:
 *   catalog        → CustomerCatalog (browse products and build a cart)
 *   cart           → PlaceOrder      (review cart and submit one order)
 *   my-orders      → MyOrders        (list + detail of past orders)
 *   report-problem → ReportProblem   (support intake form for a selected order)
 *   my-support-requests → MySupportRequests (read-only list and detail)
 *
 * The parent (main.jsx) renders this when role === 'CUSTOMER'.
 * Internal reviewer/admin views are NEVER rendered from here.
 *
 * Props
 * -----
 * api      — createApi() instance
 * identity — { user_id, role, customer_id, … }
 * key      — sessionVersion from parent resets internal state on sign-out
 */
export default function CustomerPortal({ api, identity }) {
  // Sub-view within the customer portal.
  const [portalView, setPortalView] = useState('catalog');
  const [cartItems, setCartItems] = useState([]);
  // The order selected for the support intake form.
  const [pendingOrder, setPendingOrder] = useState(null);

  function handleAddToCart(product, quantity) {
    setCartItems((items) => addProductToCart(items, product, quantity));
  }

  function handleOrderSuccess() {
    setCartItems([]);
  }

  function handleBackToCatalog() {
    setPortalView('catalog');
  }

  const cartQuantity = cartItems.reduce((total, item) => total + item.quantity, 0);

  function handleReportProblem(order) {
    setPendingOrder(order);
    setPortalView('report-problem');
  }

  function handleBackToMyOrders() {
    setPendingOrder(null);
    setPortalView('my-orders');
  }

  return (
    <div className="customer-portal">
      {/* Portal sub-navigation */}
      <nav className="portal-subnav" aria-label="Customer portal navigation">
        <button
          type="button"
          className={`portal-subnav-item${portalView === 'catalog' ? ' portal-subnav-item--active' : ''}`}
          aria-current={portalView === 'catalog' ? 'page' : undefined}
          onClick={() => setPortalView('catalog')}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
            strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
            <path d="M6 2L3 6v14h18V6l-3-4zM3 6h18M16 10a4 4 0 01-8 0" />
          </svg>
          Catalog
        </button>
        <button
          type="button"
          className={`portal-subnav-item${portalView === 'cart' ? ' portal-subnav-item--active' : ''}`}
          aria-current={portalView === 'cart' ? 'page' : undefined}
          onClick={() => setPortalView('cart')}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
            strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
            <path d="M3 3h2l2.4 11.2a2 2 0 002 1.6h7.8a2 2 0 001.9-1.4L21 7H6M10 20h.01M18 20h.01" />
          </svg>
          Cart <span className="portal-cart-count" aria-label={`${cartQuantity} items`}>{cartQuantity}</span>
        </button>
        <button
          type="button"
          className={`portal-subnav-item${portalView === 'my-orders' ? ' portal-subnav-item--active' : ''}`}
          aria-current={portalView === 'my-orders' ? 'page' : undefined}
          onClick={() => setPortalView('my-orders')}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
            strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
            <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
          </svg>
          My Orders
        </button>
        <button
          type="button"
          className={`portal-subnav-item${portalView === 'my-support-requests' ? ' portal-subnav-item--active' : ''}`}
          aria-current={portalView === 'my-support-requests' ? 'page' : undefined}
          onClick={() => setPortalView('my-support-requests')}
        >
          My Support Requests
        </button>
      </nav>

      {/* Portal views — only one is shown at a time */}
      {portalView === 'catalog' && (
        <CustomerCatalog api={api} cartItems={cartItems} onAddToCart={handleAddToCart}
          onOpenCart={() => setPortalView('cart')} />
      )}

      {portalView === 'cart' && (
        <PlaceOrder
          api={api}
          items={cartItems}
          onUpdateQuantity={(sku, quantity) => setCartItems((items) => updateCartQuantity(items, sku, quantity))}
          onRemove={(sku) => setCartItems((items) => removeCartItem(items, sku))}
          onSuccess={handleOrderSuccess}
          onCancel={handleBackToCatalog}
          onViewOrders={() => setPortalView('my-orders')}
        />
      )}

      {portalView === 'my-orders' && (
        <MyOrders api={api} onReportProblem={handleReportProblem} />
      )}

      {portalView === 'my-support-requests' && (
        <MySupportRequests api={api} />
      )}

      {portalView === 'report-problem' && pendingOrder && (
        <ReportProblem
          api={api}
          order={pendingOrder}
          onCancel={handleBackToMyOrders}
        />
      )}

      {/* If somehow we land on report-problem without an order, fall back */}
      {portalView === 'report-problem' && !pendingOrder && (
        <MyOrders api={api} onReportProblem={handleReportProblem} />
      )}
    </div>
  );
}
