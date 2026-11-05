document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.artisan-discovery-photo').forEach((photo) => {
    photo.addEventListener('error', () => {
      photo.hidden = true;
      const fallback = photo.parentElement && photo.parentElement.querySelector('.artisan-discovery-avatar-fallback');
      if (fallback) fallback.hidden = false;
    }, { once: true });
  });
});
