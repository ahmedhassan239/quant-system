<?php

namespace App\Http\Controllers;

use App\Models\TradeLog;
use Illuminate\Http\Request;

class TradeLogController extends Controller
{
    public function index()
    {
        // بنجيب أحدث 50 قرار خده البوت
        $logs = TradeLog::latest()->take(50)->get();

        return response()->json([
            'status' => 'success',
            'data' => $logs
        ]);
    }
}