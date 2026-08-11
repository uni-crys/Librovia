import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  BookMarked,
  Library,
  RefreshCw,
  Search,
  SlidersHorizontal,
  X,
} from 'lucide-react';

import BookCard from '../components/BookCard';
import FilterPanel from '../components/FilterPanel';
import { libraryService } from '../services/LibraryService';

const PLATFORM_LABELS = {
  readmoo: 'Readmoo',
  kobo: 'Kobo',
};

export default function Dashboard({ userId = 'user_01' }) {
  const [books, setBooks] = useState([]);
  const [filterOptions, setFilterOptions] = useState({
    total: 0,
    platforms: [],
    categories: [],
  });
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState('');
  const [appliedKeyword, setAppliedKeyword] = useState('');
  const [selectedPlatforms, setSelectedPlatforms] = useState([]);
  const [selectedCategories, setSelectedCategories] = useState([]);
  const [errorMsg, setErrorMsg] = useState(null);
  const [noticeMsg, setNoticeMsg] = useState(null);
  const [authRequiredPlatforms, setAuthRequiredPlatforms] = useState([]);
  const [syncingLibrary, setSyncingLibrary] = useState(false);
  const [retryingMetadata, setRetryingMetadata] = useState(false);
  const [metadataStatus, setMetadataStatus] = useState({
    manual_review: 0,
  });
  const [mobileFiltersOpen, setMobileFiltersOpen] = useState(false);
  const requestSequence = useRef(0);

  const fetchBooks = useCallback(async () => {
    const requestId = ++requestSequence.current;
    setLoading(true);
    setErrorMsg(null);
    try {
      const data = await libraryService.getBooks(userId, {
        keyword: appliedKeyword,
        platforms: selectedPlatforms,
        categories: selectedCategories,
      });
      if (requestId === requestSequence.current) {
        setBooks(data || []);
      }
    } catch (error) {
      console.error('取得書櫃資料失敗:', error);
      if (requestId === requestSequence.current) {
        setErrorMsg('暫時無法載入藏書，請確認後端服務是否正常。');
      }
    } finally {
      if (requestId === requestSequence.current) {
        setLoading(false);
      }
    }
  }, [appliedKeyword, selectedCategories, selectedPlatforms, userId]);

  const fetchFilterOptions = useCallback(async () => {
    try {
      const data = await libraryService.getBookFilters(userId);
      setFilterOptions({
        total: data?.total || 0,
        platforms: data?.platforms || [],
        categories: data?.categories || [],
      });
    } catch (error) {
      console.error('取得篩選選項失敗:', error);
      setErrorMsg('無法取得篩選選項，請重新整理頁面。');
    }
  }, [userId]);

  const fetchMetadataStatus = useCallback(async () => {
    try {
      const data = await libraryService.getMetadataStatus(userId);
      setMetadataStatus(data || { manual_review: 0 });
    } catch (error) {
      console.error('取得 metadata 狀態失敗:', error);
    }
  }, [userId]);

  useEffect(() => {
    fetchFilterOptions();
  }, [fetchFilterOptions]);

  useEffect(() => {
    fetchBooks();
  }, [fetchBooks]);

  useEffect(() => {
    fetchMetadataStatus();
  }, [fetchMetadataStatus]);

  const handleSearchSubmit = (e) => {
    e.preventDefault();
    const nextKeyword = keyword.trim();
    if (nextKeyword === appliedKeyword) {
      fetchBooks();
    } else {
      setAppliedKeyword(nextKeyword);
    }
  };

  const toggleValue = (setter) => (value) => {
    setter((current) => (
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value]
    ));
  };

  const clearFilters = () => {
    setKeyword('');
    setAppliedKeyword('');
    setSelectedPlatforms([]);
    setSelectedCategories([]);
  };

  const removeKeyword = () => {
    setKeyword('');
    setAppliedKeyword('');
  };

  const syncLibrary = async () => {
    setSyncingLibrary(true);
    setErrorMsg(null);
    setNoticeMsg(null);
    setAuthRequiredPlatforms([]);
    try {
      const result = await libraryService.importLibrary(userId);
      await Promise.all([
        fetchFilterOptions(),
        fetchBooks(),
        fetchMetadataStatus(),
      ]);
      if (result?.status === 'auth_required') {
        setAuthRequiredPlatforms(result.needs_auth || []);
        setNoticeMsg(result.message);
      } else if (
        result?.status === 'partial_failure'
        || result?.status === 'error'
      ) {
        setErrorMsg(result.message || '部分平台同步失敗。');
      } else {
        const queued = Number(result?.metadata_jobs || 0);
        const baseMessage = (
          result?.message || 'Readmoo 與 Kobo 書櫃同步完成。'
        );
        setNoticeMsg(
          queued > 0
            ? `${baseMessage} 已先顯示書籍，${queued} 筆資料正在背景補齊。`
            : baseMessage,
        );
      }
    } catch (error) {
      console.error('同步書櫃失敗:', error);
      setErrorMsg(
        error.response?.data?.detail
        || error.message
        || '書櫃同步失敗，請稍後再試。',
      );
    } finally {
      setSyncingLibrary(false);
    }
  };

  const retryMetadata = async () => {
    setRetryingMetadata(true);
    setErrorMsg(null);
    setNoticeMsg(null);
    try {
      const result = await libraryService.retryMetadata(userId);
      setNoticeMsg(result?.message || '已重新排入缺少資料的書籍。');
      await fetchMetadataStatus();
    } catch (error) {
      console.error('重試 metadata 失敗:', error);
      setErrorMsg(
        error.response?.data?.detail
        || error.message
        || '無法重新排入缺少資料的書籍。',
      );
    } finally {
      setRetryingMetadata(false);
    }
  };

  const activeFilterCount = (
    selectedPlatforms.length
    + selectedCategories.length
    + (appliedKeyword ? 1 : 0)
  );

  return (
    <main className="library-page">
      <header className="library-hero">
        <div className="library-shell library-hero__content">
          <div className="brand-mark" aria-hidden="true">
            <Library />
          </div>
          <div className="library-hero__copy">
            <p className="eyebrow">LIBROVIA · 自由書閣</p>
            <h1>藏書閣</h1>
            <p>
              The way your books come together.
              <br />
              讓分散各處的書，循同一條路回到你的書閣。
            </p>
          </div>
          <div className="library-stat">
            <BookMarked aria-hidden="true" />
            <div>
              <strong>{filterOptions.total}</strong>
              <span>本藏書</span>
            </div>
          </div>
        </div>
      </header>

      <div className="library-shell library-layout">
        <FilterPanel
          platforms={filterOptions.platforms}
          categories={filterOptions.categories}
          selectedPlatforms={selectedPlatforms}
          selectedCategories={selectedCategories}
          onTogglePlatform={toggleValue(setSelectedPlatforms)}
          onToggleCategory={toggleValue(setSelectedCategories)}
          onClear={clearFilters}
          activeCount={activeFilterCount}
          mobileOpen={mobileFiltersOpen}
          onCloseMobile={() => setMobileFiltersOpen(false)}
        />

        {mobileFiltersOpen && (
          <button
            className="filter-backdrop"
            type="button"
            aria-label="關閉篩選"
            onClick={() => setMobileFiltersOpen(false)}
          />
        )}

        <section className="library-results" aria-busy={loading}>
          <div className="library-toolbar">
            <form className="search-box" onSubmit={handleSearchSubmit}>
              <Search aria-hidden="true" />
            <input
              type="text"
                placeholder="搜尋書名或作者"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
                aria-label="搜尋書名或作者"
            />
              <button type="submit">搜尋</button>
          </form>

          <button
              type="button"
              className="mobile-filter-button"
              onClick={() => setMobileFiltersOpen(true)}
            >
              <SlidersHorizontal />
              篩選
              {activeFilterCount > 0 && <span>{activeFilterCount}</span>}
            </button>

            <button
              type="button"
              onClick={syncLibrary}
              className="library-sync-button"
              disabled={syncingLibrary}
            >
              <RefreshCw
                className={syncingLibrary ? 'is-spinning' : ''}
                aria-hidden="true"
              />
              {syncingLibrary ? '同步中…' : '同步書櫃'}
            </button>
            {Number(metadataStatus.manual_review || 0) > 0 && (
              <button
                type="button"
                onClick={retryMetadata}
                className="library-sync-button"
                disabled={retryingMetadata}
              >
                <RefreshCw
                  className={retryingMetadata ? 'is-spinning' : ''}
                  aria-hidden="true"
                />
                {retryingMetadata
                  ? '重新排入中…'
                  : `重試缺少資料 (${metadataStatus.manual_review})`}
              </button>
            )}
          </div>

          {noticeMsg && (
            <div
              className={`page-notice ${
                authRequiredPlatforms.length > 0 ? 'needs-action' : ''
              }`}
              role="status"
            >
              <span>
                {noticeMsg}
                {authRequiredPlatforms.length > 0 && (
                  <small>
                    需要更新：
                    {authRequiredPlatforms
                      .map((platform) => PLATFORM_LABELS[platform] || platform)
                      .join('、')}
                  </small>
                )}
              </span>
              {authRequiredPlatforms.length > 0 && (
                <a href="#sync">前往同步狀態</a>
              )}
              <button
                type="button"
                aria-label="關閉通知"
                onClick={() => {
                  setNoticeMsg(null);
                  setAuthRequiredPlatforms([]);
                }}
              >
                <X aria-hidden="true" />
              </button>
            </div>
          )}

          <div className="results-heading">
            <div>
              <p className="eyebrow">YOUR COLLECTION</p>
              <h2>{loading ? '正在整理書櫃…' : `${books.length} 本符合條件`}</h2>
            </div>
            {appliedKeyword && (
              <button
                type="button"
                className="active-search"
                onClick={removeKeyword}
              >
                「{appliedKeyword}」
                <X aria-hidden="true" />
              </button>
            )}
          </div>

          {errorMsg && (
            <div className="error-banner" role="alert">
          {errorMsg}
        </div>
      )}

      {loading ? (
            <div className="book-grid" aria-label="載入藏書">
              {Array.from({ length: 8 }, (_, index) => (
                <div className="book-skeleton" key={index}>
                  <div />
                  <span />
                  <span />
                </div>
              ))}
            </div>
      ) : books.length === 0 ? (
            <div className="empty-library">
              <BookMarked aria-hidden="true" />
              <h3>沒有找到符合條件的書</h3>
              <p>試著減少篩選條件，或換一個關鍵字搜尋。</p>
              {activeFilterCount > 0 && (
                <button type="button" onClick={clearFilters}>
                  清除全部條件
                </button>
              )}
        </div>
      ) : (
            <div className="book-grid">
              {books.map((book) => (
                <BookCard book={book} key={book.isbn} />
              ))}
        </div>
      )}
        </section>
      </div>
    </main>
  );
}
