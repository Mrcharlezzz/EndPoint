import { forwardRef, type InputHTMLAttributes, type SelectHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  mono?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { mono, className, ...rest },
  ref,
) {
  return <input ref={ref} className={cn('input', mono && 'mono', className)} {...rest} />;
});

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  mono?: boolean;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { mono, className, ...rest },
  ref,
) {
  return <select ref={ref} className={cn('input', mono && 'mono', className)} {...rest} />;
});
