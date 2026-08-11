import apiClient from './api';

export const libraryService = {
  // 1. 取得已購書籍清單
  getBooks: async (userId, params = {}) => {
    const query = new URLSearchParams();
    query.set('user_id', userId);
    if (params.keyword) query.set('keyword', params.keyword);
    (params.platforms || []).forEach((platform) => {
      query.append('platform', platform);
    });
    (params.categories || []).forEach((category) => {
      query.append('category', category);
    });

    const response = await apiClient.get('/books/', {
      params: query,
    });
    return response.data;
  },

  // 2. 取得該使用者實際擁有的篩選選項與數量
  getBookFilters: async (userId) => {
    const response = await apiClient.get('/books/filters', {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 3. 觸發已購書櫃同步
  importLibrary: async (userId) => {
    const response = await apiClient.post('/library/import', null, {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 4. 取得書櫃 metadata 背景補齊進度
  getMetadataStatus: async (userId) => {
    const response = await apiClient.get('/library/metadata-status', {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 5. 手動強制重試已達上限或仍缺漏的 metadata 工作
  retryMetadata: async (userId) => {
    const response = await apiClient.post('/library/metadata-retry', null, {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 6. 取得待購清單
  getWishlist: async (userId) => {
    const response = await apiClient.get('/wishlist/', {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 7. 新增書籍至待購清單
  addToWishlist: async (userId, query) => {
    const response = await apiClient.post('/wishlist/', {
      user_id: userId,
      query,
    });
    return response.data;
  },

  // 8. 觸發待購清單同步/爬取
  importWishlist: async (userId) => {
    const response = await apiClient.post('/wishlist/import', null, {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 9. 從待購清單移除書籍
  removeFromWishlist: async (isbn, userId) => {
    const response = await apiClient.delete(`/wishlist/${isbn}`, {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 10. 將一或多本待購書籍標記為已購並移入書櫃
  transferWishlistBooks: async (userId, isbns, platforms) => {
    const response = await apiClient.post('/wishlist/transfer', {
      user_id: userId,
      isbns,
      platforms,
    });
    return response.data;
  },

  // 11. 取得 Readmoo / Kobo 登入憑證狀態
  getPlatformStatus: async (userId) => {
    const response = await apiClient.get('/auth/status', {
      params: { user_id: userId },
    });
    return response.data;
  },

  // 12. 開啟平台登入視窗並儲存該使用者的新憑證
  loginPlatform: async (userId, platform) => {
    const response = await apiClient.post('/auth/login', null, {
      params: { user_id: userId, platform },
    });
    return response.data;
  },
};
