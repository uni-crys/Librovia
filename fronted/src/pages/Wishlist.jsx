import React, { useCallback, useEffect, useState } from 'react';
import {
  ArrowRight,
  BookOpen,
  Check,
  Heart,
  LoaderCircle,
  Plus,
  RefreshCw,
  ShoppingBag,
  Trash2,
  X,
} from 'lucide-react';

import { libraryService } from '../services/LibraryService';

const PLATFORM_LABELS = {
  kobo: 'Kobo',
  readmoo: 'Readmoo',
};

const STATUS_LABELS = {
  pending: '等待同步',
  synced: '已同步',
  failed: '同步失敗',
  not_available: '平台沒有此書',
  auth_expired: '登入失效',
  removed: '已移除',
};

function WishlistCover({ book }) {
  const [failed, setFailed] = useState(false);
  const valid = /^https?:\/\//i.test(book.cover_url || '') && !failed;
  return valid ? (
    <img
      src={book.cover_url}
      alt={`${book.title}封面`}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  ) : (
    <div className="wishlist-card__fallback">
      <BookOpen aria-hidden="true" />
      <span>{book.title}</span>
    </div>
  );
}

function WishlistCard({
  book,
  selected,
  onToggle,
  onTransfer,
  onRemove,
  removing,
}) {
  return (
    <article className={`wishlist-card ${selected ? 'is-selected' : ''}`}>
      <label className="wishlist-card__select">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggle}
          aria-label={`選取 ${book.title}`}
        />
        <span aria-hidden="true">{selected && <Check />}</span>
      </label>

      <div className="wishlist-card__cover">
        <WishlistCover book={book} />
      </div>

      <div className="wishlist-card__body">
        <span className="category-label">{book.category || '未分類'}</span>
        <h3>{book.title}</h3>
        <p className="wishlist-card__author">{book.author || '未知作者'}</p>

        <div className="sync-status-list">
          {(book.platforms || []).map((item) => (
            <span
              className={`sync-chip sync-chip--${item.status}`}
              key={item.platform}
            >
              <i />
              {PLATFORM_LABELS[item.platform] || item.platform}
              · {STATUS_LABELS[item.status] || item.status}
            </span>
          ))}
        </div>

        <div className="wishlist-card__actions">
          <button type="button" onClick={onTransfer}>
            移入我的書櫃
            <ArrowRight />
          </button>
          <button
            type="button"
            className="wishlist-card__remove"
            onClick={onRemove}
            disabled={removing}
            aria-label={`從待購清單移除 ${book.title}`}
          >
            {removing ? <LoaderCircle className="is-spinning" /> : <Trash2 />}
          </button>
        </div>
      </div>
    </article>
  );
}

function TransferDialog({
  books,
  platforms,
  onTogglePlatform,
  onClose,
  onConfirm,
  submitting,
}) {
  const isBulk = books.length > 1;
  const valid = platforms.length > 0 && (!isBulk || platforms.length === 1);

  return (
    <div className="dialog-backdrop" role="presentation">
      <section
        className="transfer-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="transfer-title"
      >
        <button
          className="icon-button transfer-dialog__close"
          type="button"
          onClick={onClose}
          aria-label="關閉"
        >
          <X />
        </button>
        <div className="transfer-dialog__icon">
          <ShoppingBag />
        </div>
        <p className="eyebrow">MOVE TO LIBRARY</p>
        <h2 id="transfer-title">
          將 {books.length} 本書移入書櫃
        </h2>
        <p>
          請選擇實際購買平台。這只會在 Librovia 標記為已購，
          不會代替你於平台付款。
        </p>

        <div className="transfer-platforms">
          {['kobo', 'readmoo'].map((platform) => {
            const checked = platforms.includes(platform);
            return (
              <label
                className={`transfer-platform ${checked ? 'is-selected' : ''}`}
                key={platform}
              >
                <input
                  type={isBulk ? 'radio' : 'checkbox'}
                  name={isBulk ? 'transfer-platform' : undefined}
                  checked={checked}
                  onChange={() => onTogglePlatform(platform, isBulk)}
                />
                <span>{checked && <Check />}</span>
                <strong>{PLATFORM_LABELS[platform]}</strong>
              </label>
            );
          })}
        </div>

        {isBulk && (
          <p className="transfer-dialog__note">
            批次移入多本書時，只能選擇單一平台。
          </p>
        )}

        <button
          type="button"
          className="primary-action"
          onClick={onConfirm}
          disabled={!valid || submitting}
        >
          {submitting ? (
            <>
              <LoaderCircle className="is-spinning" />
              處理中
            </>
          ) : (
            <>
              確認移入書櫃
              <ArrowRight />
            </>
          )}
        </button>
      </section>
    </div>
  );
}

export default function Wishlist({ userId }) {
  const [books, setBooks] = useState([]);
  const [selected, setSelected] = useState([]);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [dialogBooks, setDialogBooks] = useState([]);
  const [transferPlatforms, setTransferPlatforms] = useState([]);
  const [transferring, setTransferring] = useState(false);

  const fetchWishlist = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await libraryService.getWishlist(userId);
      setBooks(data || []);
      setSelected((current) => (
        current.filter((isbn) => data.some((book) => book.isbn === isbn))
      ));
    } catch (requestError) {
      console.error(requestError);
      setError('無法載入待購清單，請確認後端服務是否正常。');
    } finally {
      setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    fetchWishlist();
  }, [fetchWishlist]);

  const addBook = async (event) => {
    event.preventDefault();
    if (!query.trim()) return;
    setAdding(true);
    setError('');
    setNotice('');
    try {
      const result = await libraryService.addToWishlist(userId, query.trim());
      setQuery('');
      setNotice(result.message);
      await fetchWishlist();
    } catch (requestError) {
      setError(
        requestError.response?.data?.detail
        || '加入失敗，請稍後再試。',
      );
    } finally {
      setAdding(false);
    }
  };

  const toggleBook = (isbn) => {
    setSelected((current) => (
      current.includes(isbn)
        ? current.filter((value) => value !== isbn)
        : [...current, isbn]
    ));
  };

  const openTransfer = (targetBooks) => {
    setDialogBooks(targetBooks);
    setTransferPlatforms([]);
  };

  const toggleTransferPlatform = (platform, isBulk) => {
    if (isBulk) {
      setTransferPlatforms([platform]);
      return;
    }
    setTransferPlatforms((current) => (
      current.includes(platform)
        ? current.filter((value) => value !== platform)
        : [...current, platform]
    ));
  };

  const confirmTransfer = async () => {
    setTransferring(true);
    setError('');
    try {
      const result = await libraryService.transferWishlistBooks(
        userId,
        dialogBooks.map((book) => book.isbn),
        transferPlatforms,
      );
      setNotice(result.message);
      setDialogBooks([]);
      setTransferPlatforms([]);
      setSelected([]);
      await fetchWishlist();
    } catch (requestError) {
      setError(
        requestError.response?.data?.detail
        || '移入書櫃失敗，請稍後再試。',
      );
    } finally {
      setTransferring(false);
    }
  };

  const removeBook = async (book) => {
    setRemoving(book.isbn);
    setError('');
    try {
      const result = await libraryService.removeFromWishlist(
        book.isbn,
        userId,
      );
      setNotice(result.message);
      await fetchWishlist();
    } catch {
      setError('移除失敗，請稍後再試。');
    } finally {
      setRemoving('');
    }
  };

  const syncRemoteWishlist = async () => {
    setSyncing(true);
    setError('');
    setNotice('');
    try {
      const result = await libraryService.importWishlist(userId);
      setNotice(result.message);
      await fetchWishlist();
    } catch (requestError) {
      setError(
        requestError.response?.data?.detail
        || '無法啟動平台待購清單同步。',
      );
    } finally {
      setSyncing(false);
    }
  };

  const selectedBooks = books.filter((book) => selected.includes(book.isbn));

  return (
    <main className="subpage">
      <header className="subpage-hero">
        <div className="library-shell subpage-hero__inner">
          <div>
            <p className="eyebrow">WISHLIST</p>
            <h1>下一本，想讀什麼？</h1>
            <p>以 ISBN 或完整書名加入，Librovia 會同步至兩個平台。</p>
          </div>
          <Heart aria-hidden="true" />
        </div>
      </header>

      <div className="library-shell wishlist-page">
        <section className="wishlist-add-panel">
          <form onSubmit={addBook}>
            <div>
              <span className="eyebrow">ADD A BOOK</span>
              <h2>加入待購清單</h2>
            </div>
            <label>
              <span className="sr-only">ISBN 或書籍名稱</span>
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="輸入 ISBN 或完整書名"
              />
            </label>
            <button type="submit" disabled={adding || !query.trim()}>
              {adding ? <LoaderCircle className="is-spinning" /> : <Plus />}
              {adding ? '正在查找' : '加入清單'}
            </button>
          </form>
          <p>新增後會在背景同步 Readmoo 與 Kobo，狀態可在書卡上查看。</p>
        </section>

        {(error || notice) && (
          <div
            className={`page-notice ${error ? 'is-error' : 'is-success'}`}
            role="status"
          >
            {error || notice}
            <button
              type="button"
              onClick={() => {
                setError('');
                setNotice('');
              }}
              aria-label="關閉訊息"
            >
              <X />
            </button>
          </div>
        )}

        <div className="wishlist-heading">
          <div>
            <p className="eyebrow">SAVED FOR LATER</p>
            <h2>{loading ? '正在整理清單…' : `${books.length} 本待購書籍`}</h2>
          </div>
          <div>
            {selected.length > 0 && (
              <button
                type="button"
                className="batch-transfer-button"
                onClick={() => openTransfer(selectedBooks)}
              >
                移入所選 {selected.length} 本
                <ArrowRight />
              </button>
            )}
            <button
              className="remote-sync-button"
              type="button"
              onClick={syncRemoteWishlist}
              disabled={syncing}
            >
              <RefreshCw className={syncing ? 'is-spinning' : ''} />
              {syncing ? '正在啟動同步' : '同步平台清單'}
            </button>
          </div>
        </div>

        {loading ? (
          <div className="wishlist-grid">
            {Array.from({ length: 4 }, (_, index) => (
              <div className="wishlist-skeleton" key={index} />
            ))}
          </div>
        ) : books.length === 0 ? (
          <div className="empty-library wishlist-empty">
            <Heart aria-hidden="true" />
            <h3>待購清單還是空的</h3>
            <p>從上方輸入一本你想讀的書，建立下一段閱讀旅程。</p>
          </div>
        ) : (
          <div className="wishlist-grid">
            {books.map((book) => (
              <WishlistCard
                book={book}
                key={book.isbn}
                selected={selected.includes(book.isbn)}
                onToggle={() => toggleBook(book.isbn)}
                onTransfer={() => openTransfer([book])}
                onRemove={() => removeBook(book)}
                removing={removing === book.isbn}
              />
            ))}
          </div>
        )}
      </div>

      {dialogBooks.length > 0 && (
        <TransferDialog
          books={dialogBooks}
          platforms={transferPlatforms}
          onTogglePlatform={toggleTransferPlatform}
          onClose={() => setDialogBooks([])}
          onConfirm={confirmTransfer}
          submitting={transferring}
        />
      )}
    </main>
  );
}
