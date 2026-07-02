<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class MarketData extends Model
{
    protected $table = 'market_data';
    
    // Python engine handles timestamps, disable Eloquent's default created_at/updated_at
    public $timestamps = false;

    // Allow reading/writing all columns
    protected $guarded = [];
}
