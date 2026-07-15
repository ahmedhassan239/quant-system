<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * Run the migrations.
     */
    public function up(): void
    {
        Schema::connection('exec_15m')->table('portfolio_state', function (Blueprint $table) {
            $table->text('entry_reason')->nullable()->after('decision');
        });
    }

    /**
     * Reverse the migrations.
     */
    public function down(): void
    {
        Schema::table('portfolio_state', function (Blueprint $table) {
            //
        });
    }
};
