import { forwardRef, type InputHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

interface SearchInputProps extends InputHTMLAttributes<HTMLInputElement> {
  placeholder?: string;
  wrapperClassName?: string;
}

export const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(function SearchInput(
  { wrapperClassName, placeholder = 'Search', className, ...rest },
  ref,
) {
  return (
    <label className={cn('search-input', wrapperClassName)}>
      <input ref={ref} type="text" placeholder={placeholder} className={className} {...rest} />
    </label>
  );
});
