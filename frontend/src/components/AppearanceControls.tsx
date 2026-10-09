import { Button, ListBox, Select } from '@heroui/react';
import { Moon, Sun } from 'lucide-react';
import { useI18n, usePreferences } from '../preferences';

export default function AppearanceControls() {
  const { theme, locale, setTheme, setLocale } = usePreferences();
  const t = useI18n();
  const themeLabel =
    theme === 'light'
      ? t('切换到深色模式', 'Switch to dark mode')
      : t('切换到浅色模式', 'Switch to light mode');

  return (
    <div className="appearance-controls">
      <Button
        isIconOnly
        size="sm"
        variant="tertiary"
        className="theme-toggle"
        aria-label={themeLabel}
        onPress={() => setTheme(theme === 'light' ? 'dark' : 'light')}
      >
        {theme === 'light' ? <Moon size={16} /> : <Sun size={16} />}
      </Button>
      <Select
        className="locale-select"
        aria-label={t('界面语言', 'Interface language')}
        value={locale}
        onChange={(value) => setLocale(value === 'en' ? 'en' : 'zh')}
      >
        <Select.Trigger>
          <Select.Value />
          <Select.Indicator />
        </Select.Trigger>
        <Select.Popover className="locale-select-popover">
          <ListBox>
            <ListBox.Item id="zh" textValue="中文">
              中文
              <ListBox.ItemIndicator />
            </ListBox.Item>
            <ListBox.Item id="en" textValue="English">
              English
              <ListBox.ItemIndicator />
            </ListBox.Item>
          </ListBox>
        </Select.Popover>
      </Select>
    </div>
  );
}
