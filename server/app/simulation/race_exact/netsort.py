
from __future__ import annotations

INTROSORT_SIZE_THRESHOLD = 16


def _floor_log2_plus_one(n: int) -> int:
    result = 0
    while n >= 1:
        result += 1
        n //= 2
    return result


def sort(keys: list, cmp, items: list | None = None, index: int = 0, length: int | None = None) -> None:
    if length is None:
        length = len(keys) - index
    if length < 2:
        return

    def swap(i, j):
        if i != j:
            keys[i], keys[j] = keys[j], keys[i]
            if items is not None:
                items[i], items[j] = items[j], items[i]

    def swap_if_greater(a, b):
        if a != b and cmp(keys[a], keys[b]) > 0:
            keys[a], keys[b] = keys[b], keys[a]
            if items is not None:
                items[a], items[b] = items[b], items[a]

    def insertion_sort(lo, hi):
        for i in range(lo, hi):
            j = i
            t = keys[i + 1]
            ti = items[i + 1] if items is not None else None
            while j >= lo and cmp(t, keys[j]) < 0:
                keys[j + 1] = keys[j]
                if items is not None:
                    items[j + 1] = items[j]
                j -= 1
            keys[j + 1] = t
            if items is not None:
                items[j + 1] = ti

    def down_heap(i, n, lo):
        d = keys[lo + i - 1]
        di = items[lo + i - 1] if items is not None else None
        while i <= n // 2:
            child = 2 * i
            if child < n and cmp(keys[lo + child - 1], keys[lo + child]) < 0:
                child += 1
            if not cmp(d, keys[lo + child - 1]) < 0:
                break
            keys[lo + i - 1] = keys[lo + child - 1]
            if items is not None:
                items[lo + i - 1] = items[lo + child - 1]
            i = child
        keys[lo + i - 1] = d
        if items is not None:
            items[lo + i - 1] = di

    def heapsort(lo, hi):
        n = hi - lo + 1
        for i in range(n // 2, 0, -1):
            down_heap(i, n, lo)
        for i in range(n, 1, -1):
            swap(lo, lo + i - 1)
            down_heap(1, i - 1, lo)

    def pick_pivot_and_partition(lo, hi):
        middle = lo + ((hi - lo) >> 1)
        swap_if_greater(lo, middle)
        swap_if_greater(lo, hi)
        swap_if_greater(middle, hi)
        pivot = keys[middle]
        swap(middle, hi - 1)
        left, right = lo, hi - 1
        while left < right:
            left += 1
            while cmp(keys[left], pivot) < 0:
                left += 1
            right -= 1
            while cmp(pivot, keys[right]) < 0:
                right -= 1
            if left >= right:
                break
            swap(left, right)
        swap(left, hi - 1)
        return left

    def intro_sort(lo, hi, depth_limit):
        while hi > lo:
            size = hi - lo + 1
            if size <= INTROSORT_SIZE_THRESHOLD:
                if size == 1:
                    return
                if size == 2:
                    swap_if_greater(lo, hi)
                    return
                if size == 3:
                    swap_if_greater(lo, hi - 1)
                    swap_if_greater(lo, hi)
                    swap_if_greater(hi - 1, hi)
                    return
                insertion_sort(lo, hi)
                return
            if depth_limit == 0:
                heapsort(lo, hi)
                return
            depth_limit -= 1
            p = pick_pivot_and_partition(lo, hi)
            intro_sort(p + 1, hi, depth_limit)
            hi = p - 1

    intro_sort(index, index + length - 1, 2 * _floor_log2_plus_one(length))
