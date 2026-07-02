<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use App\Models\MarketData;
use App\Models\TradingSignal;
use Illuminate\Http\Request;

class DashboardController extends Controller
{
    public function index()
    {
        // Fetch the latest 100 records from the market_data table ordered by timestamp ASC
        $marketData = MarketData::orderBy('timestamp', 'asc')->take(100)->get();
        
        // Fetch the latest 5 records from the trading_signals table ordered by timestamp DESC
        $latestSignals = TradingSignal::orderBy('timestamp', 'desc')->take(5)->get();

        return response()->json([
            'market_data' => $marketData,
            'latest_signals' => $latestSignals
        ]);
    }
}
