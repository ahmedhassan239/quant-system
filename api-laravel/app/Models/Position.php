<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;

class Position extends Model
{
    protected $connection = 'exec_15m';
    protected $table = 'portfolio_state';
    public $timestamps = false;
}
