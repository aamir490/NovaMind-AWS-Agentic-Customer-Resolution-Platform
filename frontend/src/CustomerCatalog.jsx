import { useEffect, useMemo, useRef, useState } from 'react';
import { CATALOG_CATEGORY_LABELS, formatCatalogPrice, selectCatalogProducts } from './catalogView.js';

const FALLBACK_IMAGE = '/products/fallback.svg';

function CatalogImage({ product }) {
  const [failed, setFailed] = useState(false);
  return <div className="catalog-card-visual">
    <img src={failed ? FALLBACK_IMAGE : product.image_path}
      alt={`${product.name} product illustration`} width="640" height="420" loading="lazy"
      onError={() => setFailed(true)} />
  </div>;
}

/**
 * Product catalog browser for authenticated customers.
 *
 * Props
 * -----
 * api          — createApi() instance (required)
 * onAddToCart  — adds the selected product and quantity to the portal cart
 * onOpenCart   — opens the cart and checkout review
 */
export default function CustomerCatalog({ api, cartItems = [], onAddToCart, onOpenCart }) {
  const [products, setProducts] = useState(null);   // null = not loaded
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('all');
  const [availability, setAvailability] = useState('all');
  const [sort, setSort] = useState('featured');
  const [announcement, setAnnouncement] = useState('');
  // Per-product quantity inputs, keyed by sku.
  const [quantities, setQuantities] = useState({});
  const requestRef = useRef(0);

  useEffect(() => {
    // Reload whenever the api instance changes (new identity).
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  function load() {
    const version = ++requestRef.current;
    setLoading(true);
    setError(null);
    api.listCatalog()
      .then((data) => {
        if (requestRef.current !== version) return;
        setProducts(data);
        // Seed quantities to 1 for every available product.
        const init = {};
        data.forEach((p) => { if (p.available) init[p.sku] = 1; });
        setQuantities(init);
      })
      .catch((err) => {
        if (requestRef.current !== version) return;
        setError(err.message || 'Could not load catalog. Check the API and try again.');
      })
      .finally(() => { if (requestRef.current === version) setLoading(false); });
  }

  function handleQtyChange(product, raw) {
    const value = Math.max(1, Math.min(product.stock_quantity, 1000, parseInt(raw, 10) || 1));
    setQuantities((prev) => ({ ...prev, [product.sku]: value }));
  }

  function handleAddToCart(product) {
    const quantity = quantities[product.sku] ?? 1;
    onAddToCart?.(product, quantity);
    setAnnouncement(`${quantity} ${quantity === 1 ? 'unit' : 'units'} of ${product.name} added to cart.`);
  }

  const cartQuantity = cartItems.reduce((total, item) => total + item.quantity, 0);

  const visibleProducts = useMemo(() => products
    ? selectCatalogProducts(products, { query, category, availability, sort })
    : [], [products, query, category, availability, sort]);
  const categories = useMemo(() => products
    ? [...new Set(products.map((product) => product.category))].sort((a, b) =>
        CATALOG_CATEGORY_LABELS[a].localeCompare(CATALOG_CATEGORY_LABELS[b]))
    : [], [products]);
  const filtersActive = query.trim().length > 0 || category !== 'all'
    || availability !== 'all' || sort !== 'featured';

  function clearFilters() {
    setQuery('');
    setCategory('all');
    setAvailability('all');
    setSort('featured');
  }

  return (
    <section className="customer-catalog" aria-labelledby="catalog-heading">
      <div className="catalog-header">
        <div>
          <p className="eyebrow">Product catalog</p>
          <h2 id="catalog-heading">Browse products</h2>
          <p className="catalog-intro">Explore the current catalog and start an order from any in-stock product.</p>
        </div>
        <div className="catalog-header-actions">
          <button type="button" className="button-secondary catalog-cart-button" onClick={onOpenCart}>
            Cart <span aria-label={`${cartQuantity} items in cart`}>{cartQuantity}</span>
          </button>
          <button
            type="button"
            className="button-secondary"
            onClick={load}
            disabled={loading}
            aria-busy={loading}
          >
            {loading ? 'Loading…' : 'Refresh catalog'}
          </button>
        </div>
      </div>

      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

      {products !== null && !error && (
        <div className="catalog-controls" aria-label="Catalog filters">
          <label className="catalog-search">
            <span>Search products</span>
            <span className="catalog-search-input">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
                strokeLinecap="round" aria-hidden="true">
                <circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" />
              </svg>
              <input type="search" value={query} placeholder="Search name, SKU or description"
                onChange={(event) => setQuery(event.target.value)} />
            </span>
          </label>
          <label>
            <span>Category</span>
            <select value={category} onChange={(event) => setCategory(event.target.value)}>
              <option value="all">All categories</option>
              {categories.map((value) => <option key={value} value={value}>
                {CATALOG_CATEGORY_LABELS[value]}
              </option>)}
            </select>
          </label>
          <label>
            <span>Availability</span>
            <select value={availability} onChange={(event) => setAvailability(event.target.value)}>
              <option value="all">All products</option>
              <option value="available">In stock</option>
              <option value="unavailable">Out of stock</option>
            </select>
          </label>
          <label>
            <span>Sort by</span>
            <select value={sort} onChange={(event) => setSort(event.target.value)}>
              <option value="featured">Availability, then name</option>
              <option value="name-asc">Name: A to Z</option>
              <option value="name-desc">Name: Z to A</option>
              <option value="price-asc">Price: low to high</option>
              <option value="price-desc">Price: high to low</option>
            </select>
          </label>
          <div className="catalog-controls-footer">
            <p>Showing authoritative catalog data and current demo stock.</p>
            <p className="catalog-result-count" role="status" aria-live="polite">
              {visibleProducts.length} of {products.length} {products.length === 1 ? 'product' : 'products'}
            </p>
          </div>
        </div>
      )}

      {loading && (
        <p className="customer-notice" role="status" aria-live="polite">
          Loading product catalog…
        </p>
      )}

      {error && (
        <div className="customer-error catalog-error" role="alert">
          <div><strong>Catalog unavailable</strong><span>{error}</span></div>
          <button type="button" className="button-secondary" onClick={load}>Try again</button>
        </div>
      )}

      {!loading && !error && products === null && (
        <div className="customer-empty" role="status">
          <span className="customer-empty-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
              strokeLinejoin="round" strokeLinecap="round">
              <path d="M6 2L3 6v14h18V6l-3-4zM3 6h18M16 10a4 4 0 01-8 0" />
            </svg>
          </span>
          <h3>Catalog not loaded</h3>
          <p>Click "Refresh catalog" to load available products.</p>
        </div>
      )}

      {!loading && !error && products !== null && products.length === 0 && (
        <div className="customer-empty" role="status">
          <h3>No products available</h3>
          <p>The catalog returned no products. Try again later.</p>
        </div>
      )}

      {!loading && !error && products !== null && products.length > 0 && visibleProducts.length === 0 && (
        <div className="customer-empty" role="status">
          <span className="customer-empty-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
              strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></svg>
          </span>
          <h3>No matching products</h3>
          <p>Adjust your search or availability filter to see more of the catalog.</p>
          {filtersActive && <button type="button" className="button-secondary" onClick={clearFilters}>Clear filters</button>}
        </div>
      )}

      {!error && visibleProducts.length > 0 && (
        <ul className="catalog-grid" aria-label="Product list">
          {visibleProducts.map((product) => (
            <li key={product.id} className={`catalog-card${product.available ? '' : ' catalog-card--unavailable'}`}>
              <CatalogImage product={product} />
              <div className="catalog-card-body">
                <div className="catalog-card-meta">
                  <span className="catalog-sku">{product.sku}</span>
                  {product.available
                    ? <span className="catalog-badge catalog-badge--available">
                        {product.stock_quantity} in stock
                      </span>
                    : <span className="catalog-badge catalog-badge--unavailable">Out of stock</span>}
                </div>
                <h3 className="catalog-card-name">{product.name}</h3>
                <p className="catalog-card-description">{product.description}</p>
                <p className="catalog-card-price" aria-label={`Price: ${product.unit_price.amount} ${product.unit_price.currency}`}>
                  <strong>{formatCatalogPrice(product.unit_price)}</strong>
                </p>
              </div>
              {product.available && (
                <div className="catalog-card-action">
                  <label className="catalog-qty-label">
                    <span>Quantity</span>
                    <input
                      type="number"
                      min="1"
                      max={Math.min(1000, product.stock_quantity)}
                      step="1"
                      value={quantities[product.sku] ?? 1}
                      aria-label={`Quantity for ${product.name}`}
                      onChange={(e) => handleQtyChange(product, e.target.value)}
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() => handleAddToCart(product)}
                    aria-label={`Add ${product.name} to cart`}
                  >
                    Add to cart
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
