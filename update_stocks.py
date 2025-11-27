"""
Script to update all stock data CSV files using the Polygon/Massive API.
This script reads existing CSV files, finds the latest date, and fetches
new data from that date to today using the REST API directly.
"""

import os
import sys
import site
import csv
from datetime import datetime, timedelta
from pathlib import Path
import requests
import time

# Add user site-packages to path (for packages installed with --user)
user_site = site.getusersitepackages()
if user_site and os.path.exists(user_site):
    sys.path.insert(0, user_site)

import pandas as pd

# API Configuration
API_KEY = "ddC9N3ABVTcKX5pITlCGGMDBNi1Las8v"
API_BASE_URL = "https://api.polygon.io"
STOCKS_DIR = "data/STOCKS"

def get_latest_date_from_csv(file_path):
    """Read the CSV and return the latest date and metadata."""
    try:
        df = pd.read_csv(file_path)
        if df.empty:
            return None, None
        
        # Convert date column to datetime
        df['date'] = pd.to_datetime(df['date'])
        
        # Get the latest date
        latest_date = df['date'].max()
        
        # Get metadata from the last row (symbol, company_name, sector, industry)
        last_row = df.iloc[-1]
        metadata = {
            'symbol': last_row['symbol'],
            'company_name': last_row.get('company_name', ''),
            'sector': last_row.get('sector', ''),
            'industry': last_row.get('industry', '')
        }
        
        return latest_date, metadata
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return None, None

def fetch_stock_data(ticker, start_date, end_date):
    """Fetch stock data from Polygon API for a given date range."""
    try:
        # Convert dates to strings
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        
        # Polygon API endpoint for aggregates
        url = f"{API_BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{start_str}/{end_str}"
        
        params = {
            'apiKey': API_KEY,
            'adjusted': 'true',
            'sort': 'asc',
            'limit': 50000
        }
        
        response = requests.get(url, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        status = data.get('status', '')
        
        # Check if we have results (status can be OK, DELAYED, or other)
        if 'results' not in data or not data.get('results'):
            if status != 'OK':
                print(f"  API returned status: {status}, message: {data.get('message', 'No results available')}")
            return []
        
        # Format the results
        aggs = []
        for result in data.get('results', []):
            aggs.append({
                'timestamp': result.get('t'),  # timestamp in milliseconds
                'open': result.get('o'),
                'high': result.get('h'),
                'low': result.get('l'),
                'close': result.get('c'),
                'volume': result.get('v'),
                'vwap': result.get('vw', None),
                'transactions': result.get('n', None)
            })
        
        return aggs
    except requests.exceptions.RequestException as e:
        print(f"  HTTP error fetching data for {ticker}: {e}")
        return []
    except Exception as e:
        print(f"  Error fetching data for {ticker}: {e}")
        return []

def format_data_for_csv(aggs, metadata):
    """Format the API response data to match CSV structure."""
    formatted_rows = []
    
    for agg in aggs:
        # Convert timestamp to date
        date = datetime.fromtimestamp(agg['timestamp'] / 1000).strftime('%Y-%m-%d')
        
        row = {
            'Open': agg['open'],
            'High': agg['high'],
            'Low': agg['low'],
            'Close': agg['close'],
            'Volume': int(agg['volume']) if agg['volume'] else 0,
            'Dividends': 0.0,  # Polygon aggregates don't include dividends
            'Stock Splits': 0.0,  # Polygon aggregates don't include splits
            'symbol': metadata['symbol'],
            'company_name': metadata['company_name'],
            'sector': metadata['sector'],
            'industry': metadata['industry'],
            'date': date
        }
        formatted_rows.append(row)
    
    return formatted_rows

def update_stock_file(file_path):
    """Update a single stock CSV file with new data."""
    print(f"\nProcessing: {file_path.name}")
    
    # Get latest date and metadata
    latest_date, metadata = get_latest_date_from_csv(file_path)
    
    if latest_date is None:
        print(f"  Could not read file or file is empty. Skipping.")
        return False
    
    if metadata is None or 'symbol' not in metadata:
        print(f"  Could not extract symbol. Skipping.")
        return False
    
    symbol = metadata['symbol']
    print(f"  Symbol: {symbol}, Latest date: {latest_date.date()}")
    
    # Calculate start date (day after latest date)
    start_date = latest_date + timedelta(days=1)
    end_date = datetime.now()
    
    # Check if update is needed
    if start_date > end_date:
        print(f"  Already up to date (latest date is {latest_date.date()})")
        return True
    
    print(f"  Fetching data from {start_date.date()} to {end_date.date()}")
    
    # Fetch new data
    aggs = fetch_stock_data(symbol, start_date, end_date)
    
    if not aggs:
        print(f"  No new data available")
        return False
    
    print(f"  Fetched {len(aggs)} new records")
    
    # Format data for CSV
    new_rows = format_data_for_csv(aggs, metadata)
    
    if not new_rows:
        print(f"  No new rows to add")
        return False
    
    # Append new data to CSV
    try:
        # Read existing data
        df_existing = pd.read_csv(file_path)
        
        # Create DataFrame from new rows
        df_new = pd.DataFrame(new_rows)
        
        # Combine and sort by date
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined['date'] = pd.to_datetime(df_combined['date'])
        df_combined = df_combined.sort_values('date').reset_index(drop=True)
        
        # Remove duplicates (in case of overlap)
        df_combined = df_combined.drop_duplicates(subset=['date', 'symbol'], keep='last')
        
        # Save back to CSV
        df_combined.to_csv(file_path, index=False)
        
        print(f"  Successfully updated with {len(new_rows)} new records")
        return True
    except Exception as e:
        print(f"  Error updating file: {e}")
        return False

def main():
    """Main function to update all stock files."""
    import sys
    
    # Check for test mode (process only first N files)
    test_limit = None
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        test_limit = 3  # Test with first 3 files
        print("TEST MODE: Processing only first 3 files\n")
    
    print("Starting stock data update...")
    print(f"API Key: {API_KEY[:10]}...")
    print(f"Stocks directory: {STOCKS_DIR}\n")
    sys.stdout.flush()
    
    # Get all CSV files in STOCKS directory
    stocks_path = Path(STOCKS_DIR)
    if not stocks_path.exists():
        print(f"Error: {STOCKS_DIR} directory not found!")
        return
    
    csv_files = list(stocks_path.glob("*_data.csv"))
    
    if not csv_files:
        print(f"No CSV files found in {STOCKS_DIR}")
        return
    
    # Limit files if in test mode
    if test_limit:
        csv_files = csv_files[:test_limit]
    
    print(f"Found {len(csv_files)} stock data files to process\n")
    sys.stdout.flush()
    
    # Process each file
    success_count = 0
    error_count = 0
    skipped_count = 0
    
    for i, file_path in enumerate(csv_files, 1):
        print(f"[{i}/{len(csv_files)}] ", end="")
        
        try:
            result = update_stock_file(file_path)
            if result:
                success_count += 1
            else:
                skipped_count += 1
            
            # Rate limiting - be nice to the API (5 requests per minute for free tier)
            time.sleep(12)  # 12 seconds delay = 5 requests per minute
            
        except Exception as e:
            print(f"  Unexpected error: {e}")
            error_count += 1
    
    # Summary
    print("\n" + "="*50)
    print("Update Summary:")
    print(f"  Total files: {len(csv_files)}")
    print(f"  Successfully updated: {success_count}")
    print(f"  Skipped (no update needed or error): {skipped_count}")
    print(f"  Errors: {error_count}")
    print("="*50)

if __name__ == "__main__":
    main()
