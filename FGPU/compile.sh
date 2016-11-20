#!/bin/bash
cd `dirname $0`
if [ $# -eq 0 ]; then
  if [ -e "benchmark.cl" ]; then
    fileName="benchmark.cl"
  else
    echo "OpenCL file is needed!"
    exit 1
  fi
fi

LLC_ARGS="-march=fgpu -mcpu=fgpu32 -relocation-model=static -mattr=-fgpu-hard-float"
for option in "$@"
do
  if [ $option = "-hard-float" ]; then
    LLC_ARGS="-march=fgpu -mcpu=fgpu32 -relocation-model=static -mattr=+fgpu-hard-float"
    break
  fi
done

for fileName in "$@"
do
  if [ $fileName = "-hard-float" ]; then
    continue
  fi
  echo "Compiling "$fileName
  if [ ! -e $fileName ]; then
    echo $fileName" does not exist!"
    continue
  fi
  fileNameLen=${#fileName}
  outFileName_ll="code.ll"
  outFileName_bin="code.bin"
  cFileName="code.array"
  hFileName="code.h"
  
  ./clang -O3 -S -emit-llvm -mfloat-abi=soft -target "mips-unknown-uknown" -o $outFileName_ll -x cl $fileName &> clang.log
  if [ $? != 0 ]; then
    echo "clang failed"
    exit 1
  fi

  ./llc $LLC_ARGS -filetype=obj -o $outFileName_bin $outFileName_ll

  if [ $? != 0 ]; then
    echo "llc obj generation failed"
    exit $?
  fi


  offset="0x"$(objdump --headers $outFileName_bin | gawk -n '/text/ {print $6}')
  size="0x"$(objdump --headers $outFileName_bin | gawk -n '/text/ {print $3}')
  offset_words=$((offset/4))
  size_words=$((size/4))
  # echo $offset_words
  # echo $size_words

  text_section=$(objdump -s $outFileName_bin | 
  gawk '{if (NR > 4) {print $2; print $3; print $4; print $5}}' |
  head -$size_words)
  text_section=($text_section)

  kernel_addresses=$(
  ./llvm-objdump -d $outFileName_bin |
  awk 'FNR > 4' |
  sed -n '/^\S/ {n; p}' | 
  awk '{print "0x"substr($1, 0, length($1)-1)}'
  )

  kernel_names=$(
  ./llvm-objdump -d $outFileName_bin |
  sed -n '/^\S/ p' |
  gawk '
  { 
    if (FNR > 2)
    {
      print substr($0, 0, length($0)-1);
    }
  }')

  # for cram-mif
  function_names=($kernel_names)
  function_addresses=($kernel_addresses)
  num_functions=0
  tmp=0
  for word in "${function_names[@]}"; do
    if [ ${word:0:3} != "LBB" ]; then
      function_names[$num_functions]=${function_names[$tmp]}
      function_addresses[$num_functions]=$((function_addresses[$tmp]/4))
      # printf "${function_names[$num_functions]}\n"
      # indices[$num_functions]=$tmp
      ((num_functions++))
    fi
    ((tmp++))
  done

  function_names=(${function_names[@]:0:num_functions})
  function_addresses=(${function_addresses[@]:0:num_functions})
  function_addresses+=($size_words)

  # printf $num_functions" functions found:\n"
  # printf "%s\t\n" "${function_names[@]}"
  # echo ${function_addresses[@]}
  kernel_names=($kernel_names)
  kernel_addresses=($kernel_addresses)
  # declare -i indices
  tmp=0
  num_kernels=0
  for word in "${kernel_names[@]}"; do
    if [[ ${word:0:3} != "LBB" && ${word:0:1} != "_" ]]; then
      kernel_names[$num_kernels]=${kernel_names[$tmp]}
      kernel_addresses[$num_kernels]=$((kernel_addresses[$tmp]/4))
      # indices[$num_kernels]=$tmp
      ((num_kernels++))
    fi
    ((tmp++))
  done



  kernel_names=(${kernel_names[@]:0:num_kernels})
  kernel_addresses=(${kernel_addresses[@]:0:num_kernels})
  kernel_addresses+=($size_words)
  # printf $num_kernels" kernels found:"
  # printf "%s\t" "${kernel_names[@]}"
  # echo ${kernel_addresses[@]}
  tmp=0
  > $hFileName
  > $cFileName
  codeVecName="code"
  printf "#define " >> $hFileName
  printf "%-36s" "${codeVecName^^}_LEN" >> $hFileName
  echo "$((kernel_addresses[$num_kernels]))" >> $hFileName
  kernel_index=0
  while [ $kernel_index -lt $num_kernels ]; do
    printf "#define " >> $hFileName
    printf "%-36s" "${kernel_names[$kernel_index]^^}_POS" >> $hFileName
    echo "$((kernel_addresses[kernel_index]))" >> $hFileName
    ((kernel_index++))
  done

  word_indx=0
  kernel_index=0
  function_index=0
  while [ $word_indx -lt ${#text_section[@]} ]; do
    if [ $word_indx = ${kernel_addresses[$kernel_index]} ]; then
      ((kernel_index++))
    fi
    printf ${text_section[$word_indx]:6:2}${text_section[$word_indx]:4:2}${text_section[$word_indx]:2:2}${text_section[$word_indx]:0:2}"\n" >> $cFileName
    if [ $word_indx = ${function_addresses[$function_index]} ]; then
      ((function_index++))
    fi
    ((word_indx++))
  done

done


echo "Compiling succeeded!"
