import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SearchBar from './SearchBar';

describe('SearchBar', () => {
  it('renders search input', () => {
    render(<SearchBar onSearch={vi.fn()} loading={false} />);
    expect(screen.getByPlaceholderText(/enter search query/i)).toBeInTheDocument();
  });

  it('calls onSearch when search button clicked', () => {
    const mockOnSearch = vi.fn();
    render(<SearchBar onSearch={mockOnSearch} loading={false} />);

    const input = screen.getByPlaceholderText(/enter search query/i);
    fireEvent.change(input, { target: { value: 'test query' } });

    const button = screen.getByRole('button', { name: /search/i });
    fireEvent.click(button);

    expect(mockOnSearch).toHaveBeenCalledWith('test query');
  });
});
