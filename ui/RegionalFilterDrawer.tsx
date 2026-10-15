/**
 * @fileoverview Filter state contract (`FilterState`), year and country option
 * reconciliation helpers, and the regional filter toolbar component.
 * @jsx React.createElement
 * @jsxFrag React.Fragment
 */

import {Box, MenuItem, Select} from 'material-ui';
import * as React from 'react';
import {
  CountryListStatus,
  CountryOption,
  fetchCountriesFromBackend,
  getAllCountries,
  getAvailableYears,
  getCountryListStatus,
  getCountryNameFromIso2,
  getLatestYear,
  GwpHorizon,
  isValidYear,
  resolveCountryIdentifier,
  subscribeToCountryCache,
} from './CountryMetadata';

/** Active country, year, and GWP horizon filter selections. */
export interface FilterState {
  /**
   * Selected country: its ISO alpha-2 code, or its display name for sentinel
   * territories (see `resolveCountryIdentifier`). Empty while no country is
   * known yet, i.e. until the country list has loaded and a default country
   * was chosen (see `resolveInitialCountry` in `dashboard_geo_utils`).
   */
  readonly country: string;
  readonly year: number;
  readonly gwp?: GwpHorizon;
}

/**
 * Returns the year the year dropdown should show: the current selection when
 * the backend still lists it, otherwise the latest listed year, or null when no
 * year is published.
 */
export function reconcileSelectedYear(
  current: number,
  availableYears: readonly number[],
): number | null {
  if (availableYears.includes(current)) return current;
  return getLatestYear(availableYears);
}

/**
 * Parses a raw Material-UI Select change value into a valid four-digit integer
 * calendar year, or returns null when the value is empty, non-numeric, or out
 * of range.
 * @visibleForTesting
 */
export function parseSelectedYear(selectedYear: unknown): number | null {
  if (typeof selectedYear === 'number') {
    return isValidYear(selectedYear) ? selectedYear : null;
  }
  if (typeof selectedYear === 'string' && selectedYear.trim() !== '') {
    const parsed = Number(selectedYear);
    return isValidYear(parsed) ? parsed : null;
  }
  return null;
}

/**
 * Text shown while the country list is being fetched.
 * @visibleForTesting
 */
export const COUNTRY_LIST_LOADING_TEXT = 'Loading countries…';
/**
 * Text shown when the country list could not be fetched.
 * @visibleForTesting
 */
export const COUNTRY_LIST_ERROR_TEXT = 'Countries unavailable';

/**
 * Returns the placeholder row for an empty picker, keyed on list status.
 * @visibleForTesting
 */
export function emptyCountryListMessage(status: CountryListStatus): string {
  if (status === 'loading') return COUNTRY_LIST_LOADING_TEXT;
  if (status === 'error') return COUNTRY_LIST_ERROR_TEXT;
  return 'No country found';
}

/**
 * Resolves the display name for a selected country ISO or name against
 * available country options.
 * @visibleForTesting
 */
export function resolveSelectedCountryName(
  selectedCountry: string,
  countryList: readonly CountryOption[],
  listStatus: CountryListStatus = 'ready',
): string {
  const selUpper = (selectedCountry || '').trim().toUpperCase();
  const found = countryList.find(
    (c) =>
      resolveCountryIdentifier(c.isoA2, c.name).toUpperCase() === selUpper ||
      c.name.trim().toUpperCase() === selUpper,
  );
  if (found) return found.name;
  // Without a selected country, the label reflects the list status.
  if (!selUpper) return emptyCountryListMessage(listStatus);
  // Names come only from the backend; until it answers, a bare ISO code would
  // flash in the label, so show the loading text instead.
  if (listStatus === 'loading') {
    return COUNTRY_LIST_LOADING_TEXT;
  }
  return getCountryNameFromIso2(selectedCountry) || selectedCountry;
}

/**
 * Filters and alphabetically sorts country options matching a search substring.
 * @visibleForTesting
 */
export function filterCountryOptions(
  countryList: readonly CountryOption[],
  search: string,
): CountryOption[] {
  const normalizedSearch = search.trim().toLowerCase();
  const filtered =
    normalizedSearch === ''
      ? [...countryList]
      : countryList.filter(
          (c) =>
            (c.name ?? '').toLowerCase().includes(normalizedSearch) ||
            (c.isoA2 ?? '').toLowerCase().includes(normalizedSearch),
        );
  let isAlreadySorted = true;
  for (let i = 1; i < filtered.length; i++) {
    if (filtered[i - 1].name.localeCompare(filtered[i].name) > 0) {
      isAlreadySorted = false;
      break;
    }
  }
  return isAlreadySorted
    ? filtered
    : filtered.sort((a, b) => a.name.localeCompare(b.name));
}

/** Props for the `RegionalFilterControls` component. */
export interface RegionalFilterProps {
  readonly filters: FilterState;
  readonly onChange: (newFilters: FilterState) => void;
}

/** Props for `SearchableCountryPicker`. */
interface SearchableCountryPickerProps {
  readonly selectedCountry: string;
  readonly countryList: readonly CountryOption[];
  readonly listStatus: CountryListStatus;
  readonly onSelect: (countryIso: string) => void;
}

/**
 * Renders a searchable dropdown picker for selecting a country.
 */
const SearchableCountryPicker: React.FC<SearchableCountryPickerProps> = ({
  selectedCountry,
  countryList,
  listStatus,
  onSelect,
}) => {
  const [isOpen, setIsOpen] = React.useState(false);
  const [search, setSearch] = React.useState('');
  const [selectedIndex, setSelectedIndex] = React.useState(0);
  const containerRef = React.useRef<HTMLDivElement>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const focusTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  const selectedCountryName = React.useMemo(
    () => resolveSelectedCountryName(selectedCountry, countryList, listStatus),
    [countryList, selectedCountry, listStatus],
  );

  const sortedCountries = React.useMemo(
    () => filterCountryOptions(countryList, search),
    [countryList, search],
  );

  React.useEffect(() => {
    setSelectedIndex(0);
  }, [search, isOpen]);

  React.useEffect(() => {
    return () => {
      if (focusTimerRef.current !== null) {
        clearTimeout(focusTimerRef.current);
      }
    };
  }, []);

  React.useEffect(() => {
    const handleOutsideClick = (e: MouseEvent) => {
      if (
        containerRef.current &&
        e.target instanceof Node &&
        !containerRef.current.contains(e.target)
      ) {
        setIsOpen(false);
      }
    };
    if (isOpen) {
      document.addEventListener('mousedown', handleOutsideClick);
    }
    return () => {
      document.removeEventListener('mousedown', handleOutsideClick);
    };
  }, [isOpen]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSelectedIndex((prev) =>
        Math.min(prev + 1, sortedCountries.length - 1),
      );
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSelectedIndex((prev) => Math.max(prev - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (sortedCountries[selectedIndex]) {
        onSelect(
          resolveCountryIdentifier(
            sortedCountries[selectedIndex].isoA2,
            sortedCountries[selectedIndex].name,
          ),
        );
        setIsOpen(false);
      }
    } else if (e.key === 'Escape') {
      setIsOpen(false);
    }
  };

  const selUpper = selectedCountry.trim().toUpperCase();

  return (
    <div ref={containerRef} style={{position: 'relative', minWidth: '240px'}}>
      <Box
        onClick={() => {
          setIsOpen(!isOpen);
          if (!isOpen) {
            setSearch('');
            if (focusTimerRef.current !== null) {
              clearTimeout(focusTimerRef.current);
            }
            focusTimerRef.current = setTimeout(
              () => inputRef.current?.focus(),
              10,
            );
          }
        }}
        display="flex"
        alignItems="center"
        justifyContent="space-between"
        style={{
          height: '40px',
          padding: '0 12px',
          backgroundColor: '#ffffff',
          border: isOpen ? '2px solid #1a73e8' : '1px solid #dadce0',
          borderRadius: '6px',
          cursor: 'pointer',
          boxSizing: 'border-box',
          fontSize: '15px',
          color: '#202124',
          userSelect: 'none',
        }}>
        <span
          style={{
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            color:
              selectedCountryName === COUNTRY_LIST_LOADING_TEXT
                ? '#5f6368'
                : undefined,
          }}>
          {selectedCountryName}
        </span>
        <span style={{fontSize: '10px', marginLeft: '8px', color: '#5f6368'}}>
          {isOpen ? '▲' : '▼'}
        </span>
      </Box>

      {isOpen && (
        <Box
          style={{
            position: 'absolute',
            top: '44px',
            left: 0,
            width: '100%',
            backgroundColor: '#ffffff',
            border: '1px solid #dadce0',
            borderRadius: '6px',
            boxShadow: '0 4px 12px rgba(0, 0, 0, 0.15)',
            zIndex: 1500,
            display: 'flex',
            flexDirection: 'column',
          }}>
          <Box style={{padding: '8px', borderBottom: '1px solid #e8eaed'}}>
            <input
              ref={inputRef}
              type="text"
              placeholder="Search country..."
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
              }}
              onKeyDown={handleKeyDown}
              style={{
                width: '100%',
                boxSizing: 'border-box',
                height: '32px',
                padding: '0 8px',
                fontSize: '14px',
                border: '1px solid #dadce0',
                borderRadius: '4px',
                outline: 'none',
              }}
            />
          </Box>
          <Box style={{maxHeight: '260px', overflowY: 'auto'}}>
            {sortedCountries.length === 0 ? (
              <Box
                role="status"
                aria-live="polite"
                style={{
                  padding: '10px',
                  fontSize: '13px',
                  color: '#5f6368',
                  textAlign: 'center',
                }}>
                {countryList.length === 0
                  ? emptyCountryListMessage(listStatus)
                  : emptyCountryListMessage('ready')}
              </Box>
            ) : (
              sortedCountries.map((country, index) => {
                const countryIdentifier = resolveCountryIdentifier(
                  country.isoA2,
                  country.name,
                );
                const isSelected =
                  selUpper === countryIdentifier.toUpperCase() ||
                  selUpper === country.name.trim().toUpperCase();
                const isHighlighted = index === selectedIndex;
                return (
                  <Box
                    key={`${country.isoA2}_${country.name}`}
                    onClick={() => {
                      onSelect(countryIdentifier);
                      setIsOpen(false);
                    }}
                    onMouseEnter={() => {
                      setSelectedIndex(index);
                    }}
                    style={{
                      padding: '8px 12px',
                      fontSize: '14px',
                      color: isSelected ? '#1a73e8' : '#202124',
                      fontWeight: isSelected ? 600 : 400,
                      backgroundColor: isHighlighted
                        ? '#f1f3f4'
                        : 'transparent',
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                    }}>
                    <span>{country.name}</span>
                    {isSelected && <span style={{fontSize: '12px'}}>✓</span>}
                  </Box>
                );
              })
            )}
          </Box>
        </Box>
      )}
    </div>
  );
};

const RegionalFilterControlsInner: React.FC<RegionalFilterProps> = ({
  filters,
  onChange,
}) => {
  const [countryList, setCountryList] =
    React.useState<readonly CountryOption[]>(getAllCountries());
  const [listStatus, setListStatus] =
    React.useState<CountryListStatus>(getCountryListStatus);
  const [availableYears, setAvailableYears] = React.useState<readonly number[]>(
    () => getAvailableYears(),
  );

  React.useEffect(() => {
    let active = true;
    // The year list is fetched by the dashboard before these controls mount;
    // follow the shared cache for later refreshes.
    const unsubscribe = subscribeToCountryCache(() => {
      if (active) {
        setAvailableYears(getAvailableYears());
        setCountryList(getAllCountries());
        setListStatus(getCountryListStatus());
      }
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  React.useEffect(() => {
    const reconciled = reconcileSelectedYear(filters.year, availableYears);
    if (reconciled !== null && reconciled !== filters.year) {
      onChange({...filters, year: reconciled});
    }
  }, [availableYears, filters, onChange]);

  React.useEffect(() => {
    if (!filters.year) return;
    let active = true;
    void fetchCountriesFromBackend(filters.year)
      .then((apiList) => {
        if (active && apiList && apiList.length > 0) {
          setCountryList(apiList);
        }
      })
      .catch((err: unknown) => {
        console.warn('Failed to update country list:', err);
      });
    return () => {
      active = false;
    };
  }, [filters.year]);

  const handleCountrySelect = (newCountry: string) => {
    onChange({...filters, country: newCountry});
  };

  const handleYearChange = (event: React.ChangeEvent<{value: unknown}>) => {
    const selectedYear = parseSelectedYear(event.target.value);
    if (selectedYear !== null) {
      onChange({...filters, year: selectedYear});
    }
  };

  const handleGwpChange = (event: React.ChangeEvent<{value: unknown}>) => {
    const selectedGwp = event.target.value;
    if (
      selectedGwp === 'GWP20' ||
      selectedGwp === 'GWP50' ||
      selectedGwp === 'GWP100'
    ) {
      onChange({...filters, gwp: selectedGwp});
    }
  };

  return (
    <Box display="flex" alignItems="center" style={{gap: '24px'}}>
      {/* Country searchable dropdown */}
      <Box display="flex" flexDirection="column">
        <span
          style={{
            fontSize: '13px',
            fontWeight: 500,
            color: '#5f6368',
            marginBottom: '4px',
          }}>
          country
        </span>
        <SearchableCountryPicker
          selectedCountry={filters.country}
          countryList={countryList}
          listStatus={listStatus}
          onSelect={handleCountrySelect}
        />
      </Box>

      {/* Year dropdown */}
      <Box display="flex" flexDirection="column">
        <span
          style={{
            fontSize: '13px',
            fontWeight: 500,
            color: '#5f6368',
            marginBottom: '4px',
          }}>
          year
        </span>
        <Select
          variant="outlined"
          value={filters.year}
          onChange={handleYearChange}
          style={{
            backgroundColor: '#ffffff',
            borderRadius: '6px',
            height: '40px',
            fontSize: '15px',
            minWidth: '120px',
          }}>
          {[...availableYears]
            .sort((a, b) => b - a)
            .map((yr) => (
              <MenuItem key={yr} value={yr}>
                {yr}
              </MenuItem>
            ))}
        </Select>
      </Box>

      {/* GWP dropdown */}
      <Box display="flex" flexDirection="column">
        <span
          style={{
            fontSize: '13px',
            fontWeight: 500,
            color: '#5f6368',
            marginBottom: '4px',
          }}>
          GWP version
        </span>
        <Select
          variant="outlined"
          value={filters.gwp ?? 'GWP50'}
          onChange={handleGwpChange}
          style={{
            backgroundColor: '#ffffff',
            borderRadius: '6px',
            height: '40px',
            fontSize: '15px',
            minWidth: '120px',
          }}>
          <MenuItem value="GWP20">GWP20</MenuItem>
          <MenuItem value="GWP50">GWP50</MenuItem>
          <MenuItem value="GWP100">GWP100</MenuItem>
        </Select>
      </Box>
    </Box>
  );
};

/**
 * Renders the filter toolbar and searchable country dropdown for selecting the
 * active country/region, calendar year, and GWP time horizon.
 */
export const RegionalFilterControls = React.memo(RegionalFilterControlsInner);
