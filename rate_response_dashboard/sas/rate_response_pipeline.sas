/**********************************Macro 1.1 - Step1: Read Mailfile Macro*********************************/
%macro readmailfile_trm(startdate,ds);

%let enddate = %sysfunc(intnx(month,"&startdate."d,0,E),date9.);
%put &enddate.;
/*Read the mailfile*/
data &ds._mailfiles;
    set datalake.experianprescreen (keep=
CAMPAIGN_CODE
CAMPAIGN_DATE
CAMPFLAG1
CAMPFLAG2
CRS18_P
CRS18_SEGMENT
ENCRYPTED_PIN
EXP_RESPONSE_SCORE
NRM16 NRM16_TIER NRM16_TIER_VS4
RISK_SCORE
RISK_SCORE_TYPE
RESERVATION_SEQUENCE
OFFER_CODE_FIRST_7
OFFER_CODE_LAST_3
response_model_type
TRM10_Score TRM10_TIER prospect_type
SCORE_VANTAGE_VALUE_V3 SCORE_VANTAGE_VALUE_V3_SEGMENT TM12_3BR  TIMES_MAILED_12MO_CNT SURNAME FIRST_NAME STREET_NUMBER DOB endmark);

where  "&startdate."d<=CAMPAIGN_DATE<="&enddate."d and
CAMPFLAG1  in ('V','A','D','M','N','R','W','C','B','G','I','J','Q')
and endmark not like '%S%';

vantage3 = score_vantage_value_v3*1;
TRM_Score = TRM10_Score*1;
EXP_RESPONSE_SCORE_num = EXP_RESPONSE_SCORE*1;
ReservationNumber = cat(OFFER_CODE_FIRST_7,RESERVATION_SEQUENCE);
TM12_3RR = TM12_3BR *1;

if campflag1 in ('V','A','D','M','N','R','W','C') then rm_flag=0;
if campflag1 in ('B','G','I','J','Q') then rm_flag=1;

run;

%mend readmailfile_trm;


/**********************************Macro 1.2 - Step2: get response from caps data base********************/
%macro getresponse_trm(startdate,ds);

%let minappdate = %sysfunc(intnx(month,"&startdate."d,-1,B),date9.);
%let maxappdate = %sysfunc(intnx(month,"&startdate."d,3,E),date9.);
%let campstartdate = %sysfunc(intnx(month,"&startdate."d,-1,B),date9.);
%let pqappdate = %sysfunc(intnx(month,"&startdate."d,-2,B),date9.);

%let frauddecs =
'Declined: Identity Verification', 'Declined: OFAD'
,'Declined: Precise ID KIQ Grading Decision Code Check'
,'Failed Fraud Check SSN Validation'
,'Failed Fraud Match Letter Sent'
,'Failed IP Fraud Score Check'
,'Failed Precise ID Decision Code Check'
,'Failed Precise ID KIQ Grading Decision Code Check'
,'Failed Red Flag - Existing App/Acct Match'
,'PQ Failed IP Fraud Check'
,'PQ Failed IP Velocity'
,'Precise ID Information: Failed to Complete Information Retrieval'
,'Device ID Decline'
,'PQ Device Id Cancel'
,'PQ Device Id Decline'
,'2nd device ID Decline'
,'Precise ID Device Evaluation Information: Failed to Complete Information Retrieval'
,'Failed Precise ID Device Evaluation Decision Code Check'
,'Failed Precise ID Device Evaluation KIQ Grading Decision Code Check'
,'Skip PQ Device Id Cancel'
,'Skip Prequal Failed IP Fraud Score Check'
,'Skip PQ Failed IP Velocity'
,'Declined: Potential Identity Theft'
,'CrossCore Failed Fraud Shield 5'
,'CrossCore Failed Fraud Shield 13'
,'CrossCore Failed Fraud Shield 14'
,'CrossCore Failed Fraud Shield 25'
,'CrossCore Failed Exception Score 9001'
,'CrossCore Failed Exception Score 9013'
,'CrossCore Mitek Id Authentication Failed'
,'Fail CIP Checks Crosscore Solicited'
,'Fail PreciseID Checks Crossscore'
,'Sentilink Check Failed'
,'Failed Negative List Check'
,'Declined: Failed Hotfile Check'
,'Crosscore Verification Failed'
;

proc sql;
    select distinct quote(strip(substr(OFFER_CODE_FIRST_7,1,7)))
    into :list_camp_codes separated by ","
    from &ds._mailfiles
    where CAMPFLAG1  in ('V','A','D','M','N','R','W','C');
quit;

proc sql;
    select distinct quote(strip(substr(OFFER_CODE_FIRST_7,1,7)))
    into :list_re_camp_codes separated by ","
    from &ds._mailfiles
    where CAMPFLAG1  in ('B','G','I','J','Q');
quit;

proc sort data= &ds._mailfiles nodupkey out = &ds._mailfiles_dm1; by ReservationNumber; run;

proc sql;
    create table &ds._dm as
    select
        a. APPLICATION_ID,
        b. DOB,
        c. RESERVATION_NUMBER,
        substr(c. RESERVATION_NUMBER, 1, 7) as CampaignCode,
        datepart(a. APPLICATION_RECEIVE_DATE) as AppRcvDate format date9.,
        datepart(a. READY_TO_BOARD_DATE) as RTBDate format date9.,
        datepart(a. BOARDDATE) as BoardDate format date9.,
        c. STATUS_DESC,
        c. STATUS_NAME,
        case when c. STATUS_DESC in (&frauddecs.) then 1 else 0 end as FraudDecline_dm,
        case when c. STATUS_DESC ^= 'Declined: Expired Offer' then 1 else 0 end as GrossResponse_dm,
        case when a. BOARDDATE > "&startdate."d then 1 else 0 end as NetResponse_dm
    from
        CAPSRPT.V_APPLICATION_CI a
        left join CAPSRPT.V_APPLICANT_CI b
            on a.APPLICATION_ID = b.APPLICATION_ID
        left join CAPSRPT.V_APPLICATION_STATUS c
            on a. APPLICATION_ID = c. APPLICATION_ID
    where
        "&maxappdate."d>= a.APPLICATION_RECEIVE_DATE >= "&minappdate."d
        and (calculated CampaignCode in (&list_camp_codes.) or calculated CampaignCode in (&list_re_camp_codes.))
    order by c. RESERVATION_NUMBER, a. APPLICATION_RECEIVE_DATE;
quit;

proc sort data = &ds._dm nodupkey out = &ds._dm1; by RESERVATION_NUMBER; run;

%mend getresponse_trm;


/*************************Macro 1.3 - Step3. combined response, apply final response logic *******************/
%macro finalresponse_trm(startdate,ds);

libname UnixFile "/sasdata/unix/Risk_Credit/Data_Strategy/ProgramCode_Dashboard/Data";

proc sql;
    create table &ds._responseds as
    select
        a. *,
        b. AppRcvDate,
        b. BoardDate,
        b. FraudDecline_dm as FraudDecline,
        b. GrossResponse_dm as GrossResponse,
        b. NetResponse_dm as NetResponse,
        c. finalcombinedaf as annual_fee
    from &ds._mailfiles_dm1 a
        left join &ds._dm1 b
            on a. ReservationNumber = b. RESERVATION_NUMBER
        left join unixfile.v_programcode c
            on substr(a.offer_code_first_7,1,4) = c.ProgramCode
;
quit;

data &ds._finalresponse;
    set &ds._responseds;

    if vantage3<640  and prospect_type="Prospecting" then scorecard =1;
    if vantage3>=640 and prospect_type="Prospecting" then scorecard =2;
    if vantage3<640  and prospect_type="Retargeting" then scorecard =3;
    if vantage3>=640 and prospect_type="Retargeting" then scorecard =4;

    if vantage3 >= 530 and vantage3 <=549 then vs_band="530-549";
    else if vantage3 >=550 and vantage3 <=600 then vs_band="550-600";
    else if vantage3 >=601 and vantage3 <=639 then vs_band="601-639";
    else if vantage3 >=640 and vantage3 <=700 then vs_band="640-700";
    else if vantage3 >=701 and vantage3 <=730 then vs_band="701-730";
    else if vantage3 >= 731 then vs_band="731-830";

    if campflag1 in ('F', 'V', 'E') then prospectingflag = 1; else prospectingflag = 0;
    if campflag1 in ('A', 'W') then pqabandon_flag = 1; else pqabandon_flag = 0;
    if campflag1 in ('C','G') then prchargeoff_flag = 1; else prchargeoff_flag = 0;
    if campflag1 in ('N', 'R','B','J') then prclosure_flag = 1; else prclosure_flag = 0;
    if campflag1 in ('D', 'M','Q') then prdecline_flag = 1; else prdecline_flag = 0;
run;

%mend finalresponse_trm;


%macro assign_psi_tier;
if TRM10_Score >=0.02908089 and TRM10_Score <=1 then total_decile=1;
else if TRM10_Score >=0.0178773 and TRM10_Score <=0.02908088 then total_decile=2;
else if TRM10_Score >=0.01394068 and TRM10_Score <=0.01787729 then total_decile=3;
else if TRM10_Score >=0.01147906 and TRM10_Score <=0.01394067 then total_decile=4;
else if TRM10_Score >=0.00966261 and TRM10_Score <=0.01147905 then total_decile=5;
else if TRM10_Score >=0.00832388 and TRM10_Score <=0.0096626 then total_decile=6;
else if TRM10_Score >=0.00726601 and TRM10_Score <=0.00832387 then total_decile=7;
else if TRM10_Score >=0.00636179 and TRM10_Score <=0.007266 then total_decile=8;
else if TRM10_Score >=0.00522037 and TRM10_Score <=0.00636178 then total_decile=9;
else if TRM10_Score >=0 and TRM10_Score <=0.00522036 then total_decile=10;
%mend;


%macro rollup(startdate,ds);
proc sql;
    create table &ds._rollup as
        select Prospect_type,
            pqabandon_flag,
            prchargeoff_flag,
            prclosure_flag,
            prdecline_flag,
            vs_band,
            annual_fee,
            times_mailed_12mo_cnt,
            trm10_tier,
            scorecard,
            rm_flag,
            count(*) as volume,
            sum(GrossResponse) as responders,
            sum(TRM_Score) as expected_responses,
            sum(EXP_RESPONSE_SCORE_num) as expected_responses_xpm,
            calculated responders/calculated volume as GRR,
            sum(NetResponse) as Boards,
            calculated Boards/calculated volume as NRR
        from trm.&ds._finalresponse
            group by
                Prospect_type, pqabandon_flag, prchargeoff_flag,
                prclosure_flag, prdecline_flag, vs_band, annual_fee,
                times_mailed_12mo_cnt, trm10_tier, scorecard, rm_flag
            order by
                Prospect_type, pqabandon_flag, prchargeoff_flag,
                prclosure_flag, prdecline_flag, vs_band, annual_fee,
                times_mailed_12mo_cnt, trm10_tier, scorecard, rm_flag;
quit;
%mend;


/* Equal-volume decile rollups for the Rank Order tab.
   - sc_decile : within each scorecard, equal-volume 10-tile by TRM_Score
   - port_decile: across the whole portfolio, equal-volume 20-tile
   PSI tiers (total_decile, sc1..sc4_decile) computed in %assign_psi_tier
   remain untouched and still live on trm.&ds._finalresponse — they're just
   no longer the basis for the dashboard rank-order analytics.
*/
%macro rank_deciles(ds);
proc sort data=trm.&ds._finalresponse;
    by scorecard;
run;
proc rank data=trm.&ds._finalresponse out=trm.&ds._finalresponse
          groups=10 descending;
    var TRM_Score;
    by scorecard;
    ranks sc_decile;
run;
proc rank data=trm.&ds._finalresponse out=trm.&ds._finalresponse
          groups=20 descending;
    var TRM_Score;
    ranks port_decile;
run;
data trm.&ds._finalresponse;
    set trm.&ds._finalresponse;
    /* proc rank groups output is 0-based; shift to 1..N. Null score rows
       keep null deciles and are filtered out by the rollup queries. */
    if not missing(sc_decile)   then sc_decile   = sc_decile   + 1;
    if not missing(port_decile) then port_decile = port_decile + 1;
run;
%mend rank_deciles;

%macro rollup_decile_sc(ds);
proc sql;
    create table &ds._decile_sc as
        select scorecard,
               sc_decile          as decile,
               count(*)           as volume,
               sum(GrossResponse) as responders,
               sum(NetResponse)   as Boards
        from trm.&ds._finalresponse
        where sc_decile is not null
        group by scorecard, sc_decile
        order by scorecard, sc_decile;
quit;
%mend rollup_decile_sc;

%macro rollup_decile_port(ds);
proc sql;
    create table &ds._decile_port as
        select port_decile        as decile,
               count(*)           as volume,
               sum(GrossResponse) as responders,
               sum(NetResponse)   as Boards
        from trm.&ds._finalresponse
        where port_decile is not null
        group by port_decile
        order by port_decile;
quit;
%mend rollup_decile_port;


%macro run_one_month(reportdate);
    %local starttime monthdate reportmon startdate labelname reptname;
    %let starttime = %sysfunc(datetime());
    %put NOTE: Start time = %sysfunc(putn(&starttime, datetime19.));
    %let monthdate = %sysfunc(intnx(month, "&reportdate"d, 0, b));
    %let reportmon = %sysfunc(putn(&monthdate, monname3.));
    %let startdate = %sysfunc(putn(&monthdate, date9.));
    %let labelname = %sysfunc(putn(&monthdate, monyy5.));
    %let reptname  = %sysfunc(putn(&monthdate, monyy7.));
    %put NOTE: Running month=&labelname startdate=&startdate reportmon=&reportmon reptname=&reptname;
    options validvarname=any;
    %readmailfile_trm(&startdate., &labelname.);
    %getresponse_trm(&startdate., &labelname.);
    %finalresponse_trm(&startdate., &labelname.);
    data trm.&labelname._finalresponse;
        set &labelname._finalresponse;
        %assign_psi_tier;
    run;
    %rollup(&startdate., &labelname.);
    proc export data=&labelname._rollup
        outfile="&folder./exp_&labelname._rollup.csv"
        dbms=csv
        replace;
    run;
    /* Equal-volume decile rollups for the Rank Order tab.
       Pure additive — main exp_&labelname._rollup.csv above is unaffected. */
    %rank_deciles(&labelname.);
    %rollup_decile_sc(&labelname.);
    proc export data=&labelname._decile_sc
        outfile="&folder./exp_&labelname._decile_sc.csv"
        dbms=csv
        replace;
    run;
    %rollup_decile_port(&labelname.);
    proc export data=&labelname._decile_port
        outfile="&folder./exp_&labelname._decile_port.csv"
        dbms=csv
        replace;
    run;
%mend;

%macro run_months(start=01JAN2025, end=01APR2026);
    %local i n monthdate reportdate;
    %let n = %sysfunc(intck(month, "&start"d, "&end"d));
    %do i = 0 %to &n;
        %let monthdate = %sysfunc(intnx(month, "&start"d, &i, b));
        %let reportdate = %sysfunc(putn(&monthdate, date9.));
        %run_one_month(&reportdate.);
    %end;
%mend;
