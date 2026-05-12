import { Input, Button, Space } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { useState } from 'react';

interface SearchBarProps {
  onSearch: (query: string) => void;
  loading?: boolean;
}

export default function SearchBar({ onSearch, loading }: SearchBarProps) {
  const [query, setQuery] = useState('');

  const handleSearch = () => {
    if (query.trim()) {
      onSearch(query);
    }
  };

  return (
    <Space.Compact style={{ width: '100%' }}>
      <Input
        placeholder="Enter search query"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onPressEnter={handleSearch}
        size="large"
      />
      <Button
        type="primary"
        icon={<SearchOutlined />}
        onClick={handleSearch}
        loading={loading}
        size="large"
      >
        Search
      </Button>
    </Space.Compact>
  );
}
