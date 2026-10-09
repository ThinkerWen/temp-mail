import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nProvider } from 'react-aria';
import App from './App';
import { initializePreferences, usePreferences } from './preferences';
import './styles.css';

initializePreferences();

function LocalizedApp() {
  const { locale } = usePreferences();
  return (
    <I18nProvider locale={locale === 'zh' ? 'zh-CN' : 'en-US'}>
      <App />
    </I18nProvider>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <LocalizedApp />
  </StrictMode>,
);
